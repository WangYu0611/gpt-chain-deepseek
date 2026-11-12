"""Main development host adapter for the shared deferred-reload coordinator.

No controller lock or native child process is touched by a request. The helper
is a separately owned Task Scheduler action and uses the same proven pause,
identity, durable-intent and crash-reconciliation protocol as the art host.
"""
from __future__ import annotations

import time

from . import art_reload
from .background import BackgroundManager


def _manager(config_path, manager=None):
    manager = manager or BackgroundManager(config_path)
    if manager.config.get("art_pipeline"):
        raise ValueError("主后台重载不能用于独立美术配置。")
    manager._reload_lane = "development"
    return manager


def request_reload(config_path, *, timeout_seconds=7200, manager=None, launch=True, now=time.time):
    return art_reload.request_reload(config_path, timeout_seconds=timeout_seconds,
        manager=_manager(config_path, manager), launch=launch, now=now)


def reload_status(config_path, *, manager=None):
    return art_reload.reload_status(config_path, manager=_manager(config_path, manager))


def run_helper(config_path, request_id, *, manager=None, now=time.time, sleep=time.sleep):
    return art_reload.run_helper(config_path, request_id, manager=_manager(config_path, manager), now=now, sleep=sleep)
