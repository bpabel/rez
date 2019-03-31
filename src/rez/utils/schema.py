"""
Utilities for working with dict-based schemas.
"""
from schema.schema import Schema, Optional, Use, And
import six

# an alias which just so happens to be the same number of characters as
# 'Optional' so that our schema are easier to read
Required = Schema


def schema_keys(schema):
    """Get the string values of keys in a dict-based schema.

    Non-string keys are ignored.

    Returns:
        Set of string keys of a schema which is in the form (eg):

            schema = Schema({Required("foo"): int,
                             Optional("bah"): basestring})
    """
    def _get_leaf(value):
        if isinstance(value, Schema):
            return _get_leaf(value._schema)
        return value

    keys = set()
    dict_ = schema._schema
    assert isinstance(dict_, dict)

    for key in dict_.iterkeys():
        key_ = _get_leaf(key)
        if isinstance(key_, basestring):
            keys.add(key_)

    return keys


def dict_to_schema(schema_dict, required, allow_custom_keys=True, modifier=None):
    """Convert a dict of Schemas into a Schema.

    Args:
        required (bool): Whether to make schema keys optional or required.
        allow_custom_keys (bool, optional): If True, creates a schema that
            allows custom items in dicts.
        modifier (callable): Functor to apply to dict values - it is applied
            via `Schema.Use`.

    Returns:
        A `Schema` object.
    """
    if modifier:
        modifier = Use(modifier)

    def _to(value):
        if isinstance(value, dict):
            d = {}
            for k, v in six.iteritems(value):
                if isinstance(k, basestring):
                    k = Required(k) if required else Optional(k)
                d[k] = _to(v)
            if allow_custom_keys:
                d[Optional(basestring)] = modifier or object
            schema = Schema(d)
        elif modifier:
            schema = And(value, modifier)
        else:
            schema = value
        return schema

    return _to(schema_dict)
