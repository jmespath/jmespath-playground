from unittest import mock
from io import StringIO

import boto3
from pytest import fixture, raises

from chalicelib.storage import Config
from chalicelib.storage import S3Storage
from chalicelib.storage import CachingStorage
from chalicelib.storage import Storage
from chalicelib.storage import MaxSizeError


def test_config_create():
    c = Config(bucket='foo', prefix='key')
    assert c.bucket == 'foo'
    assert c.prefix == 'key'


@fixture
def mock_client():
    s3 = boto3.client(
        's3', region_name='us-west-2',
        aws_access_key_id='access_key',
        aws_secret_access_key='secret_key'
    )
    client = mock.Mock(spec=s3)
    return client


@fixture
def mock_storage():
    return mock.Mock(spec=Storage)


@fixture
def fake_client():
    return FakeS3Client()


class FakeS3Client:
    def __init__(self):
        self.state = {}

    def put_object(self, Bucket, Key, Body):
        bucket_state = self.state.setdefault(Bucket, {})
        bytes_body = self._get_bytes_body(Body)
        bucket_state[Key] = bytes_body

    def get_object(self, Bucket, Key):
        bucket_state = self.state.setdefault(Bucket, {})
        return {
            'Body': StringIO(bucket_state[Key]),
        }

    def _get_bytes_body(self, body):
        if hasattr(body, 'read'):
            return body.read()
        return body


class TestS3Storage:
    def setup_method(self):
        self.config = Config(bucket='bucket',
                             prefix='prefix')
        self.input_data = {'query': 'foo',
                           'input': {'foo': 'bar'}}

    # Note that for these tests we don't actually care what the
    # input_data is so it's pulled up into an instance attr.
    # We just care if you put data you can get() the same
    # data back later.

    def test_can_put_and_get_new_object(self, fake_client):
        storage = S3Storage(fake_client, self.config)
        uid = storage.put(self.input_data)
        retrieved = storage.get(uid)
        assert retrieved == self.input_data
        assert 'bucket' in fake_client.state
        keys = list(fake_client.state['bucket'].keys())
        assert len(keys) == 1, keys
        assert keys[0].startswith('prefix/')

    def test_can_put_and_get_with_no_prefix(self, fake_client):
        config = Config(bucket='bucket')
        storage = S3Storage(fake_client, config)
        uid = storage.put(self.input_data)
        retrieved = storage.get(uid)
        assert retrieved == self.input_data
        assert uid in fake_client.state['bucket']

    def test_can_put_and_get_with_slash_prefix(self, fake_client):
        config = Config(bucket='bucket', prefix='slash/')
        storage = S3Storage(fake_client, config)
        uid = storage.put(self.input_data)
        retrieved = storage.get(uid)
        assert retrieved == self.input_data
        assert list(fake_client.state['bucket'].keys()) == ['slash/%s' % uid]

    def test_can_validate_max_body_size(self, fake_client):
        config = Config(bucket='bucket', max_body_size=15)
        under_max_size = {"foo": "bar"}
        over_max_size = {"foo": {"bar": {"baz": "qux"}}}

        storage = S3Storage(fake_client, config)
        uid = storage.put(under_max_size)
        with raises(MaxSizeError):
            storage.put(over_max_size)
        assert list(fake_client.state['bucket'].keys()) == [uid]


class TestCachingStorage:
    def test_not_in_cache_calls_real_storage(self, mock_storage):
        cache = {}
        mock_storage.get.return_value = {'foo': 'bar'}
        storage = CachingStorage(mock_storage, cache)
        assert storage.get('uuid') == {'foo': 'bar'}
        mock_storage.get.assert_called_with('uuid')

    def test_real_storage_not_called_if_in_cache(self, mock_storage):
        cache = {'uuid': {'foo': 'bar'}}
        storage = CachingStorage(mock_storage, cache)
        assert storage.get('uuid') == {'foo': 'bar'}
        assert not mock_storage.get.called

    def test_subsequent_gets_are_cached(self, mock_storage):
        cache = {}
        mock_storage.get.return_value = {'foo': 'bar'}
        storage = CachingStorage(mock_storage, cache)
        assert storage.get('uuid') == {'foo': 'bar'}
        assert storage.get('uuid') == {'foo': 'bar'}
        # We only need to call the real storage object once.
        # Subsequent requests pull from the cache.
        assert mock_storage.get.call_count == 1
        assert 'uuid' in cache

    def test_assert_put_inserts_in_cache(self, mock_storage):
        cache = {}
        mock_storage.put.return_value = 'returned-uuid'
        storage = CachingStorage(mock_storage, cache)
        uuid = storage.put({'foo': 'bar'})
        assert uuid == 'returned-uuid'
        # This should retrieve from the cache, no get()
        # call is made to the real storage object.
        assert storage.get('returned-uuid') == {'foo': 'bar'}
        assert not mock_storage.get.called
