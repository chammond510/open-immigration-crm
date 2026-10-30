"""The AI features the CRM offers, each off until a superuser turns it on."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    # True for work that runs without a staff member waiting, through the firm's
    # background connection; otherwise it runs through the asking staff member's choice.
    background: bool = False


_features = {}


def register(feature):
    _features[feature.key] = feature
    return feature


def get(key):
    return _features.get(key)


def all_features():
    return sorted(_features.values(), key=lambda feature: feature.label)


CONNECTION_TEST = "connection_test"


def label_for(key):
    """A feature's name for the request log, including the built-in connection test."""
    if key == CONNECTION_TEST:
        return "Connection test"
    feature = get(key)
    return feature.label if feature else key
