import argparse

from troposphere import Ref, Template, Parameter, Output, Sub, Join
from troposphere import AccountId
from troposphere import codebuild, s3, iam, kms, codepipeline, sns, events
from awacs.aws import Policy, Statement, Allow, Action, Principal
# To avoid collisions with the services imported
# from troposphere, the policy actions are imported with
# a leading underscore
from awacs.helpers.trust import make_simple_assume_policy
from awacs import sts as _sts
from awacs import logs as _logs
from awacs import s3 as _s3
from awacs import kms as _kms
from awacs import sns as _sns
from awacs import cloudformation as _cfn


class PipelineTemplate(object):

    PARAMS = {
        'ProdActionRoleArn': Parameter(
            'ProdActionRoleArn',
            Type='String',
            Description='Role ARN to use for codepipeline deploys to prod'
        ),
        'ProdDeployRoleArn': Parameter(
            'ProdDeployRoleArn',
            Type='String',
            Description='Role ARN to use for CFN deploys to prod'
        ),
        'ApplicationName': Parameter(
            'ApplicationName',
            Default='jmespath-playground',
            Type='String',
            Description='Enter the name of your application.'
        ),
        'CodeBuildImage': Parameter(
            'CodeBuildImage',
            Default='python:3.6.1',
            Type='String',
            Description='Name of codebuild image to use.'
        ),
        'GithubPersonalToken': Parameter(
            'GithubPersonalToken',
            Type='String',
            Description='Personal access token for github repo access'
        ),
        'ProdAccountId': Parameter(
            'ProdAccountId',
            Type='String',
            Description='Account Id of Prod Account'
        )
    }

    PROD_ACCOUNT_PRINCIPAL = Principal(
        'AWS', Join('', ['arn:aws:iam::',
                         PARAMS['ProdAccountId'].Ref(),
                         ':root'])
    )

    DEPLOY_ACCOUNT_PRINCIPAL = Principal(
        'AWS', Join('', ['arn:aws:iam::', AccountId, ':root']))

    def __init__(self):
        self._t = Template()
        self._t.version = '2010-09-09'

    def _add_parameters(self, include_prod):
        params = self.PARAMS.copy()
        if not include_prod:
            del params['ProdActionRoleArn']
            del params['ProdDeployRoleArn']
            del params['ProdAccountId']
        self._t.add_parameter(list(params.values()))

    def generate_template(self, args):
        self._add_parameters(args.prod)
        t = self._t
        artifact_bucket_store = self._create_artifact_bucket_store(args.prod)
        app_bucket = self._create_app_bucket(args.prod)

        cfn_deploy_role = self._create_cfn_deploy_role()
        deploy_key = self._create_kms_deploy_key(args.prod)
        code_build_role = self._create_code_build_role(deploy_key)

        app_package_build = self._create_codebuild_project(code_build_role,
                                                           deploy_key)
        pipeline_role = self._create_pipeline_role(deploy_key)

        stages = self._create_pipeline_stages(app_package_build,
                                              cfn_deploy_role, args.prod)
        pipeline = self._create_code_pipeline(
            pipeline_role, artifact_bucket_store, stages, deploy_key)
        if args.pipeline_notifications:
            self._add_pipeline_notifications(pipeline)

        t.add_output([
            Output('S3PipelineBucket', Value=Ref(artifact_bucket_store)),
            Output('CodePipelineRoleArn', Value=pipeline_role.GetAtt('Arn')),
            Output('CodeBuildRoleArn', Value=code_build_role.GetAtt('Arn')),
            Output('CFNDeployRoleArn', Value=cfn_deploy_role.GetAtt('Arn')),
            Output('S3ApplicationBucket', Value=Ref(app_bucket)),
        ])
        return t

    def _create_cfn_deploy_role(self):
        cfn_deploy_role = iam.Role(
            'CFNDeployRole',
            AssumeRolePolicyDocument=self._allow_assume_role_service(
                'cloudformation'
            ),
            Policies=[
                iam.PolicyProperty(
                    PolicyName='DeployAccess',
                    PolicyDocument=Policy(
                        Version='2012-10-17',
                        Statement=[
                            Statement(
                                Action=[Action('*')],
                                Resource=['*'],
                                Effect=Allow,
                            )
                        ]
                    )
                )
            ]
        )
        self._t.add_resource(cfn_deploy_role)
        return cfn_deploy_role

    def _create_code_build_role(self, deploy_key):
        code_build_role = iam.Role(
            'CodeBuildRole',
            AssumeRolePolicyDocument=Policy(
                Version='2012-10-17',
                Statement=[
                    Statement(
                        Effect=Allow,
                        Action=[
                            _sts.AssumeRole,
                        ],
                        Principal=Principal(
                            'Service', 'codebuild.amazonaws.com'
                        )
                    )
                ]
            )
        )
        self._t.add_resource(code_build_role)
        code_build_policy = iam.PolicyType(
            'CodeBuildPolicy',
            PolicyName='CodeBuildPolicy',
            PolicyDocument=Policy(
                Version='2012-10-17',
                Statement=[
                    Statement(
                        Action=[
                            _logs.CreateLogGroup,
                            _logs.CreateLogStream,
                            _logs.PutLogEvents,
                        ],
                        Effect=Allow,
                        Resource=['*'],
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            _s3.GetObject,
                            _s3.GetObjectVersion,
                            _s3.PutObject,
                        ],
                        Resource=[_s3.ARN('*')],
                    ),
                    Statement(
                        Effect=Allow,
                        Action=[
                            _kms.Action('*'),
                        ],
                        Resource=[deploy_key.GetAtt('Arn')],
                    ),
                ]
            ),
            Roles=[Ref(code_build_role)],
        )
        self._t.add_resource(code_build_policy)
        return code_build_role

    def _create_kms_deploy_key(self, include_prod):
        statements = [
            Statement(
                Sid='AdminAccess',
                Effect=Allow,
                Principal=self.DEPLOY_ACCOUNT_PRINCIPAL,
                Action=[_kms.Action('*')],
                Resource=['*'],
            )
        ]
        if include_prod:
            statements.append(
                Statement(
                    Sid='KeyUsage',
                    Effect=Allow,
                    Principal=self.PROD_ACCOUNT_PRINCIPAL,
                    Action=[
                        _kms.Encrypt,
                        _kms.Decrypt,
                        Action('kms', 'ReEncrypt*'),
                        _kms.GenerateDataKey,
                        _kms.DescribeKey
                    ],
                    Resource=['*'],
                )
            )
        deploy_key = kms.Key(
            'DeployKey',
            KeyPolicy=Policy(
                Version='2012-10-17',
                Id='KeyPolicyId',
                Statement=statements,
            )
        )
        self._t.add_resource(deploy_key)
        return deploy_key

    def _create_app_bucket(self, include_prod):
        # This is where the s3 deployment packages
        # from the 'aws cloudformation package' command
        # are uploaded.  We should investigate just using
        # the artifact_bucket_store with a different prefix
        # instead of requiring two buckets.
        app_bucket = s3.Bucket('ApplicationBucket')
        self._t.add_resource(app_bucket)
        if include_prod:
            app_bucket_policy = s3.BucketPolicy(
                'ApplicationBucketPolicy',
                Bucket=app_bucket.Ref(),
                PolicyDocument=Policy(
                    Version='2012-10-17',
                    Statement=[
                        Statement(
                            Effect=Allow,
                            Action=[_s3.Action('*')],
                            Principal=self.PROD_ACCOUNT_PRINCIPAL,
                            Resource=[
                                Join('',
                                     ['arn:aws:s3:::',
                                      app_bucket.Ref(),
                                      '/*'])
                            ]
                        )
                    ],
                )
            )
            self._t.add_resource(app_bucket_policy)
        return app_bucket

    def _create_pipeline_role(self, deploy_key):
        pipeline_role = iam.Role(
            'CodePipelineRole',
            AssumeRolePolicyDocument=self._allow_assume_role_service(
                'codepipeline',
            ),
            Policies=[
                iam.PolicyProperty(
                    PolicyName='DefaultPolicy',
                    PolicyDocument=Policy(
                        Version='2012-10-17',
                        Statement=[
                            Statement(
                                Action=[
                                    _s3.GetObject,
                                    _s3.GetObjectVersion,
                                    _s3.GetBucketVersioning,
                                    _s3.CreateBucket,
                                    _s3.PutObject,
                                    _s3.PutBucketVersioning,
                                ],
                                Resource=['*'],
                                Effect=Allow,
                            ),
                            Statement(
                                Action=[
                                    Action('cloudwatch', '*'),
                                    Action('iam', 'PassRole'),
                                    Action('iam', 'ListRoles'),
                                    Action('iam', 'GetRole'),
                                    _sts.AssumeRole,
                                ],
                                Resource=['*'],
                                Effect=Allow,
                            ),
                            Statement(
                                Action=[
                                    Action('lambda', 'InvokeFunction'),
                                    Action('lambda', 'ListFunctions'),
                                ],
                                Resource=['*'],
                                Effect=Allow,
                            ),
                            Statement(
                                Action=[
                                    _cfn.CreateStack,
                                    _cfn.DeleteStack,
                                    _cfn.DescribeStacks,
                                    _cfn.UpdateStack,
                                    _cfn.CreateChangeSet,
                                    _cfn.DeleteChangeSet,
                                    _cfn.DescribeChangeSet,
                                    _cfn.ExecuteChangeSet,
                                    _cfn.SetStackPolicy,
                                    _cfn.ValidateTemplate,
                                    Action('iam', 'PassRole'),
                                ],
                                Resource=['*'],
                                Effect=Allow,
                            ),
                            Statement(
                                Action=[
                                    Action('codebuild', 'BatchGetBuilds'),
                                    Action('codebuild', 'StartBuild'),
                                ],
                                Resource=['*'],
                                Effect=Allow,
                            ),
                            Statement(
                                Action=[
                                    Action('kms', '*'),
                                ],
                                Resource=[deploy_key.GetAtt('Arn')],
                                Effect=Allow,
                            ),
                        ]
                    )
                )
            ]
        )
        self._t.add_resource(pipeline_role)
        return pipeline_role

    def _create_code_pipeline(self, pipeline_role, artifact_bucket_store,
                              stages, deploy_key):
        pipeline = codepipeline.Pipeline(
            'AppPipeline',
            Name=Sub('${ApplicationName}-pipeline'),
            RoleArn=pipeline_role.GetAtt('Arn'),
            Stages=stages,
            ArtifactStore=codepipeline.ArtifactStore(
                Type='S3',
                Location=artifact_bucket_store.Ref(),
                EncryptionKey=codepipeline.EncryptionKey(
                    Id=deploy_key.Ref(),
                    Type='KMS',
                )
            )
        )
        self._t.add_resource(pipeline)
        return pipeline

    def _create_pipeline_stages(self, app_package_build, cfn_deploy_role,
                                include_prod):
        stages = [
            codepipeline.Stages(
                Name='Source',
                Actions=[
                    codepipeline.Actions(
                        Name='Source',
                        RunOrder=1,
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Source',
                            Owner='ThirdParty',
                            Version='1',
                            Provider='GitHub',
                        ),
                        Configuration={
                            'Owner': 'jamesls',
                            'Repo': 'jmespath-playground',
                            'PollForSourceChanges': True,
                            'OAuthToken': self.PARAMS[
                                'GithubPersonalToken'].Ref(),
                            'Branch': 'master',
                        },
                        OutputArtifacts=[
                            codepipeline.OutputArtifacts(
                                Name='SourceRepo',
                            )
                        ]
                    )
                ]
            ),
            codepipeline.Stages(
                Name='Build',
                Actions=[
                    codepipeline.Actions(
                        Name='CodeBuild',
                        RunOrder=1,
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Build',
                            Owner='AWS',
                            Version='1',
                            Provider='CodeBuild',
                        ),
                        Configuration={
                            'ProjectName': app_package_build.Ref(),
                        },
                        InputArtifacts=[
                            codepipeline.InputArtifacts(
                                Name='SourceRepo',
                            )
                        ],
                        OutputArtifacts=[
                            codepipeline.OutputArtifacts(
                                Name='CompiledCFNTemplate',
                            )
                        ]
                    )
                ]
            ),
            codepipeline.Stages(
                Name='Beta',
                Actions=[
                    codepipeline.Actions(
                        Name='CreateBetaChangeSet',
                        RunOrder=1,
                        InputArtifacts=[
                            codepipeline.InputArtifacts(
                                Name='CompiledCFNTemplate',
                            )
                        ],
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Deploy',
                            Owner='AWS',
                            Version='1',
                            Provider='CloudFormation',
                        ),
                        Configuration={
                            'ActionMode': 'CHANGE_SET_REPLACE',
                            'ChangeSetName': Sub(
                                '${ApplicationName}-change-set'),
                            'RoleArn': cfn_deploy_role.GetAtt('Arn'),
                            'Capabilities': 'CAPABILITY_IAM',
                            'StackName': Sub('${ApplicationName}-beta-stack'),
                            'TemplateConfiguration': (
                                'CompiledCFNTemplate::dev-params.json'),
                            'TemplatePath': (
                                'CompiledCFNTemplate::transformed.yaml')
                        }
                    ),
                    codepipeline.Actions(
                        Name='ExecuteChangeSet',
                        RunOrder=2,
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Deploy',
                            Owner='AWS',
                            Version='1',
                            Provider='CloudFormation',
                        ),
                        OutputArtifacts=[
                            codepipeline.OutputArtifacts(
                                Name='AppDeploymentValues',
                            )
                        ],
                        Configuration={
                            "StackName": Sub("${ApplicationName}-beta-stack"),
                            "ActionMode": "CHANGE_SET_EXECUTE",
                            "ChangeSetName": Sub(
                                "${ApplicationName}-change-set"),
                            "OutputFileName": "StackOutputs.json"
                        }
                    ),
                ]
            )
        ]
        if not include_prod:
            return stages
        prod_stages = [
            codepipeline.Stages(
                Name='ApproveProd',
                Actions=[
                    codepipeline.Actions(
                        Name='ApproveProdDeploy',
                        RunOrder=1,
                        InputArtifacts=[],
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Approval',
                            Owner='AWS',
                            Version='1',
                            Provider='Manual',
                        ),
                        Configuration={
                            'CustomData': 'Approve to deploy to prod.',
                        }
                    )
                ]
            ),
            codepipeline.Stages(
                Name='Prod',
                Actions=[
                    codepipeline.Actions(
                        Name='CreateProdChangeSet',
                        RunOrder=1,
                        InputArtifacts=[
                            codepipeline.InputArtifacts(
                                Name='CompiledCFNTemplate',
                            )
                        ],
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Deploy',
                            Owner='AWS',
                            Version='1',
                            Provider='CloudFormation',
                        ),
                        RoleArn=self.PARAMS['ProdActionRoleArn'].Ref(),
                        Configuration={
                            'ActionMode': 'CHANGE_SET_REPLACE',
                            'ChangeSetName': Sub(
                                '${ApplicationName}-change-set-prod'),
                            'RoleArn': self.PARAMS['ProdDeployRoleArn'].Ref(),
                            'Capabilities': 'CAPABILITY_IAM',
                            'StackName': Sub('${ApplicationName}-prod-stack'),
                            'TemplateConfiguration': (
                                'CompiledCFNTemplate::prod-params.json'),
                            'TemplatePath': (
                                'CompiledCFNTemplate::transformed.yaml')
                        }
                    ),
                    codepipeline.Actions(
                        Name='ExecuteChangeSet',
                        RunOrder=2,
                        ActionTypeId=codepipeline.ActionTypeID(
                            Category='Deploy',
                            Owner='AWS',
                            Version='1',
                            Provider='CloudFormation',
                        ),
                        RoleArn=self.PARAMS['ProdActionRoleArn'].Ref(),
                        Configuration={
                            "StackName": Sub("${ApplicationName}-prod-stack"),
                            "ActionMode": "CHANGE_SET_EXECUTE",
                            "ChangeSetName": Sub(
                                "${ApplicationName}-change-set-prod"),
                            'RoleArn': self.PARAMS['ProdDeployRoleArn'].Ref(),
                        }
                    ),
                ]
            )
        ]
        return stages + prod_stages

    def _create_codebuild_project(self, code_build_role, deploy_key):
        app_package_build = codebuild.Project(
            'AppPackageBuild',
            Artifacts=codebuild.Artifacts(
                Type='CODEPIPELINE'
            ),
            Name=Sub('${ApplicationName}-build'),
            Environment=codebuild.Environment(
                ComputeType='BUILD_GENERAL1_SMALL',
                Image=Ref('CodeBuildImage'),
                Type='LINUX_CONTAINER',
                EnvironmentVariables=[
                    codebuild.EnvironmentVariable(
                        Name='APP_S3_BUCKET',
                        Value=Ref('ApplicationBucket')
                    )
                ]
            ),
            ServiceRole=code_build_role.GetAtt('Arn'),
            EncryptionKey=deploy_key.GetAtt('Arn'),
            Source=codebuild.Source(Type='CODEPIPELINE'),
        )
        self._t.add_resource(app_package_build)
        return app_package_build

    def _create_artifact_bucket_store(self, include_prod):
        # Bucket where all the artifacts are stored while going
        # through the CodePipeline.
        artifact_bucket_store = s3.Bucket('ArtifactBucketStore')
        self._t.add_resource(artifact_bucket_store)
        if include_prod:
            artifact_bucket_policy = s3.BucketPolicy(
                'ArtifactBucketStorePolicy',
                Bucket=artifact_bucket_store.Ref(),
                PolicyDocument=Policy(
                    Version='2012-10-17',
                    Statement=[
                        Statement(
                            Effect=Allow,
                            Principal=self.PROD_ACCOUNT_PRINCIPAL,
                            Action=[_s3.Action('*')],
                            Resource=[
                                Join('',
                                     ['arn:aws:s3:::',
                                      artifact_bucket_store.Ref(),
                                      '/*'])
                            ],
                        )
                    ]
                )
            )
            self._t.add_resource(artifact_bucket_policy)
        return artifact_bucket_store

    def _allow_assume_role_service(self, service_name):
        # make_simple_assume_policy does not add a version number,
        # so this is a wrapper that injects the version string.
        policy = make_simple_assume_policy(
            '%s.amazonaws.com' % service_name,
        )
        policy.Version = '2012-10-17'
        return policy

    def _add_pipeline_notifications(self, pipeline):
        subscriptions = self._create_sns_subscriptions()
        topic = sns.Topic(
            'AppPipelineDeployments',
            DisplayName='AppPipelineDeployments',
            Subscription=subscriptions,
        )
        self._t.add_resource(topic)

        topic_policy = sns.TopicPolicy(
            'AllowCloudWatchEventsPublish',
            PolicyDocument=Policy(
                Version='2012-10-17',
                Statement=[
                    Statement(
                        Sid='AllowCloudWatchEventsToPublish',
                        Effect=Allow,
                        Action=[_sns.Publish],
                        Principal=Principal('Service', 'events.amazonaws.com'),
                        Resource=[topic.Ref()],
                    )
                ]
            ),
            Topics=[topic.Ref()],
        )
        self._t.add_resource(topic_policy)

        sns_target = [events.Target(Id='1', Arn=topic.Ref())]
        cw_event = events.Rule(
            'PipelineEvents',
            Description='CloudWatch Events Rule for app pipeline.',
            EventPattern={
                "source": [
                    "aws.codepipeline"
                ],
                "detail-type": [
                    'CodePipeline Action Execution State Change',
                ],
                'detail': {
                    'type': {
                        # Notify when a deploy fails/succeeds
                        # or when an approval is needed.
                        # We could also add something when any
                        # part of the pipeline fails.
                        'category': ['Deploy', 'Approval'],
                    },
                    'pipeline': [pipeline.Ref()],
                }
            },
            Targets=sns_target,
        )
        self._t.add_resource(cw_event)
        return topic

    def _create_sns_subscriptions(self):
        protocol = Parameter(
            'SNSProtocol',
            Type='String',
            Description=('The protocol type for the SNS subscription used '
                         'for pipeline event notifications (e.g "email")')
        )
        endpoint = Parameter(
            'SNSEndpoint',
            Type='String',
            Description=('The endpoint value for the SNS subscription used '
                         'for pipeline event notifications.  If the protocol '
                         'type is "email" then this value is the email '
                         'address.')
        )
        self._t.add_parameter([protocol, endpoint])
        return [
            sns.Subscription(
                Protocol=protocol.Ref(),
                Endpoint=endpoint.Ref(),
            )
        ]


def generate_template(args):
    return PipelineTemplate().generate_template(args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prod', action='store_true', default=True,
                        help='Add cross account prod deployments.')
    parser.add_argument('--no-prod', action='store_false',
                        dest='prod', default=True,
                        help='Add cross account prod deployments.')
    parser.add_argument('--pipeline-notifications', action='store_true',
                        help=('Add notifications when the pipeline either '
                              'deploys successfully or fails.  This will add '
                              'params for the protocol and endpoint of the '
                              'SNS subscription.'))
    args = parser.parse_args()
    t = generate_template(args)
    print(t.to_json(indent=2))


if __name__ == '__main__':
    main()
