from marshmallow import Schema, fields


class SavedQuery(Schema):
    # We could potentially verify that this is
    # a valid JMESPath expression.
    query = fields.Str(required=True)
    data = fields.Field(required=True)
