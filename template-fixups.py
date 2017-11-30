import argparse
import json


class CFNTemplate:
    def __init__(self, data):
        self._data = data

    def add_parameters(self, parameters):
        self._data.setdefault('Parameters', {}).update(parameters)

    def get_parameter_default(self, name):
        return self._data.get('Parameters', {}).get(name, {}).get('Default')

    def resources(self, resource_type=None):
        for value in self._data['Resources'].values():
            if resource_type is not None:
                if value['Type'] != resource_type:
                    continue
            yield value

    def to_json(self):
        return json.dumps(self._data, indent=2, separators=(',', ': ')) + '\n'


def fixup_template(fileobj):
    template = CFNTemplate(json.load(fileobj))
    extract_lambda_env_vars_to_template_params(template)
    extract_bucket_reference_for_param_reference(template, 'AppS3Bucket')
    return template


def extract_lambda_env_vars_to_template_params(template):
    # This pulls out the lambda environment variable as
    # cloudformation parameters.  That way they can be
    # overriden when deploying the stack.  This will have
    # no functional difference if you don't override these
    # values.
    extracted_template_params = {}
    for resource in template.resources('AWS::Serverless::Function'):
        env = resource['Properties'].get('Environment')
        if env is None:
            continue
        env_vars = resource['Properties']['Environment']['Variables']
        for key in env_vars:
            # This isn't safe in the general case because we
            # could have name collisions, but in our case
            # we know we're using UPPER_SNAKE_CASE so
            # we won't have collisions.
            param_key = to_camel_case(key)
            extracted_template_params[param_key] = {
                'Default': env_vars[key],
                'Type': 'String',
            }
            env_vars[key] = {'Ref': param_key}
    template.add_parameters(extracted_template_params)


def to_camel_case(key):
    return ''.join([k.capitalize() for k in key.split('_')])


def extract_bucket_reference_for_param_reference(template, param_name):
    # This is a specific change for this app (vs. the pull up lambda
    # env vars as template params).  We want to replace the hard
    # coded references to our S3 bucket in our IAM policy with
    # the CFN param value that we've extracted.
    param_value = template.get_parameter_default(param_name)
    if param_value is None:
        return
    for resource in template.resources('AWS::Serverless::Function'):
        policies = resource['Properties'].get('Policies')
        if policies is None:
            continue
        for policy in policies:
            for statement in policy['Statement']:
                if param_value not in statement.get('Resource', ''):
                    continue
                old_value = statement['Resource']
                parts = list(old_value.partition(param_value))
                parts[1] = {'Ref': param_name}
                new_value = {'Fn::Join': ["", parts]}
                statement['Resource'] = new_value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('template', type=argparse.FileType('r'))
    parser.add_argument('-i', '--inplace', action='store_true',
                        help=('Rewrite the template in place.  If this '
                              'value is not provided, the new template is '
                              'written to stdout.'))
    args = parser.parse_args()
    new_template = fixup_template(args.template)
    template_json = new_template.to_json()
    if not args.inplace:
        print(template_json)
    else:
        with open(args.template.name, 'w') as f:
            f.write(template_json)


main()
