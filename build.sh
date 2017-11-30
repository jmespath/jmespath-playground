#!/bin/bash
pip install --upgrade awscli
aws --version
pip install virtualenv
virtualenv /tmp/venv
. /tmp/venv/bin/activate
pip install -r requirements-dev.txt
pip install chalice
make check || exit 1
make test || exit 1

chalice package /tmp/packaged
python template-fixups.py -i /tmp/packaged/sam.json
aws cloudformation package --template-file /tmp/packaged/sam.json --s3-bucket "${APP_S3_BUCKET}" --output-template-file /tmp/packaged/transformed.yaml
cp config/*.json /tmp/packaged/
