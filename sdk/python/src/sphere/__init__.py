"""SPHERE — metered AI gateway client (§4.7 of BUTTERBASE_BACKEND_DESIGN.md)."""

from ._client import (
    APIError,
    Client,
    InsufficientCreditsError,
    InvalidKeyError,
    InvalidRequestError,
    ModelNotFoundError,
    SphereError,
)

__all__ = [
    "Client",
    "SphereError",
    "APIError",
    "InsufficientCreditsError",
    "InvalidKeyError",
    "InvalidRequestError",
    "ModelNotFoundError",
]
__version__ = "0.1.0"
