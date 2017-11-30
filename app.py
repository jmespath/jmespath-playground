import os

import boto3


from chalice import Chalice, BadRequestError
from chalicelib.storage import Config, S3Storage, MaxSizeError, CachingStorage
from chalicelib.storage import SemiDBMCache
from chalicelib.schema import SavedQuery


CACHE_DIR = '/tmp/appcache'

app = Chalice(app_name='jmespath-playground')
app.debug = True
app.context = {}


def before_request(app):
    if 'storage' in app.context:
        return
    s3 = boto3.client('s3')
    config = Config(
        bucket=os.environ['APP_S3_BUCKET'],
        prefix=os.environ.get('APP_S3_PREFIX', ''),
    )
    if not os.path.isdir(CACHE_DIR):
        os.makedirs(CACHE_DIR)
    cache = SemiDBMCache(CACHE_DIR)
    storage = S3Storage(client=s3,
                        config=config)
    app.context['storage'] = CachingStorage(storage, cache)


@app.route('/anon', methods=['POST'], cors=True)
def new_anonymous_query():
    before_request(app)
    try:
        body = app.current_request.json_body
    except ValueError as e:
        raise BadRequestError("Invalid JSON: %s" % e)
    _validate_body(body)
    storage = app.context['storage']
    try:
        uuid = storage.put(body)
    except MaxSizeError as e:
        raise BadRequestError(str(e))
    return {'uuid': uuid}


def _validate_body(body):
    if body is None:
        raise BadRequestError("Request body cannot be empty.")
    data = SavedQuery().load(body)
    if data.errors:
        raise BadRequestError(data.errors)


@app.route('/anon/{uuid}', methods=['GET'], cors=True)
def get_anonymous_query(uuid):
    before_request(app)
    storage = app.context['storage']
    result = storage.get(uuid)
    return result


# This is just used as a sanity check to make sure
# we can hit our API.  Could also be used for monitoring.
@app.route('/ping', methods=['GET'], cors=True)
def ping():
    return {'ping': 10}
