import pytest


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", default=False, help="Run slow profiling tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--slow"):
        return
    skip = pytest.mark.skip(reason="slow profiling test — pass --slow to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
