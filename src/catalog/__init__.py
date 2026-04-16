"""Component catalog — load, validate, query, and serialize catalog/*.json."""

from .models import (
    Body, Cap, Mounting, Pin, PinGroup, Component,
    ValidationError, CatalogResult,
)
from .loader import load_catalog, get_component, CATALOG_DIR
from .serialization import catalog_to_dict, component_to_dict, component_to_design_dict, resolve_config

# Back-compat alias (old code used the underscore-prefixed name)
_component_to_dict = component_to_dict

__all__ = [
    # Models
    "Body", "Cap", "Mounting", "Pin", "PinGroup", "Component",
    "ValidationError", "CatalogResult",
    # Loader
    "load_catalog", "get_component", "CATALOG_DIR",
    # Serialization
    "catalog_to_dict", "component_to_dict", "component_to_design_dict",
    "resolve_config", "_component_to_dict",
]
