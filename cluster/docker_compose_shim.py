"""Pre-import shim: strip the deprecated `ssl_version=...` kwarg that
the docker-py shipped on this host (v1.29 compose) keeps passing to
the newer `docker.utils.kwargs_from_env`. Runs at interpreter startup
before compose imports happen.
"""
import functools


def _wrap_kwargs_from_env(orig):
    @functools.wraps(orig)
    def wrapped(environment=None, **kwargs):
        kwargs.pop("ssl_version", None)
        return orig(environment=environment, **kwargs)
    return wrapped


def install():
    try:
        from docker.utils import kwargs_from_env
    except ImportError:
        return
    import docker.utils as du
    if getattr(du.kwargs_from_env, "_shimmed", False):
        return
    du.kwargs_from_env = _wrap_kwargs_from_env(kwargs_from_env)
    du.kwargs_from_env._shimmed = True


install()
