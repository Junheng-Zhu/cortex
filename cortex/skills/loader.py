from importlib import import_module


def load_skill(path: str) -> object:
    module_name, attribute = path.rsplit(":", 1)
    return getattr(import_module(module_name), attribute)
