#
# Copyright (c) Granulate. All rights reserved.
# Licensed under the AGPL3 License. See LICENSE.md in the project root for license information.
#
import json
import os
import shutil
import signal
import stat
from functools import lru_cache
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, cast

import psutil
import requests
from granulate_utils.linux.ns import get_proc_root_path, get_process_nspid, resolve_proc_root_links, run_in_ns
from granulate_utils.linux.process import is_musl, is_process_running
from retry import retry
from websocket import create_connection
from websocket._core import WebSocket

from gprofiler.log import get_logger_adapter
from gprofiler.metadata.versions import get_exe_version
from gprofiler.utils import TEMPORARY_STORAGE_PATH, add_permission_dir
from gprofiler.utils.fs import resource_path
from gprofiler.utils.process import pgrep_exe

logger = get_logger_adapter(__name__)


class NodeDebuggerUrlNotFound(Exception):
    pass


class NodeDebuggerUnexpectedResponse(Exception):
    pass


def _get_node_major_version(process: psutil.Process) -> str:
    node_version = get_exe_version(process, Event(), 3)
    # i. e. v16.3.2 -> 16
    return node_version[1:].split(".")[0]


@lru_cache(maxsize=1)
def _get_dso_git_rev() -> str:
    libc_dso_version_file = resource_path(os.path.join("node", "module", "glibc", "version"))
    musl_dso_version_file = resource_path(os.path.join("node", "module", "musl", "version"))
    libc_dso_ver = Path(libc_dso_version_file).read_text()
    musl_dso_ver = Path(musl_dso_version_file).read_text()
    # with no build errors this should always be the same
    assert libc_dso_ver == musl_dso_ver
    return libc_dso_ver


@lru_cache()
def _get_dest_inside_container(musl: bool, node_version: str) -> str:
    libc = "musl" if musl else "glibc"
    return os.path.join(TEMPORARY_STORAGE_PATH, "node_module", _get_dso_git_rev(), libc, node_version)


def _start_debugger(pid: int) -> None:
    # for windows: in shell node -e "process._debugProcess(PID)"
    os.kill(pid, signal.SIGUSR1)


@retry(NodeDebuggerUrlNotFound, 5, 1)
def _get_debugger_url() -> str:
    # when killing process with SIGUSR1 it will open new debugger session on port 9229,
    # so it will always the same. When another debugger is opened in same NS it will not open new one.
    # REF: Inspector agent initialization uses host_port
    # https://github.com/nodejs/node/blob/5fad0b93667ffc6e4def52996b9529ac99b26319/src/inspector_agent.cc#L668
    # host_port defaults to 9229
    # ref: https://github.com/nodejs/node/blob/2849283c4cebbfbf523cc24303941dc36df9332f/src/node_options.h#L90
    # in our case it won't be changed
    port = 9229
    debugger_url_response = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=3)
    if debugger_url_response.status_code != 200 or "application/json" not in debugger_url_response.headers.get(
        "Content-Type", ""
    ):
        raise NodeDebuggerUrlNotFound(
            {"status_code": debugger_url_response.status_code, "text": debugger_url_response.text}
        )

    response_json = debugger_url_response.json()
    if (
        not isinstance(response_json, list)
        or len(response_json) == 0
        or not isinstance(response_json[0], dict)
        or "webSocketDebuggerUrl" not in response_json[0]
    ):
        raise NodeDebuggerUrlNotFound(response_json)

    return cast(str, response_json[0]["webSocketDebuggerUrl"])


def _send_socket_request(sock: WebSocket, cdp_request: Dict) -> None:
    sock.send(json.dumps(cdp_request))
    message = sock.recv()
    try:
        message = json.loads(message)
    except json.JSONDecodeError:
        raise NodeDebuggerUnexpectedResponse(message)

    if (
        "result" not in message.keys()
        or "result" not in message["result"].keys()
        or "type" not in message["result"]["result"].keys()
        or message["result"]["result"]["type"] != "boolean"
    ):
        raise NodeDebuggerUnexpectedResponse(message)


def _execute_js_command(sock: WebSocket, command: str) -> Any:
    cdp_request = {
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {
            "expression": command,
            "replMode": True,
        },
    }
    sock.send(json.dumps(cdp_request))
    message = sock.recv()
    try:
        message = json.loads(message)
    except json.JSONDecodeError:
        raise NodeDebuggerUnexpectedResponse(message)
    try:
        return message["result"]["result"]["value"]
    except KeyError:
        raise NodeDebuggerUnexpectedResponse(message)


def _change_dso_state(sock: WebSocket, module_path: str, action: str) -> None:
    assert action in ("start", "stop"), "_change_dso_state supports only start and stop actions"
    cdp_request = {
        "id": 1,
        "method": "Runtime.evaluate",
        "params": {
            "expression": f'process.mainModule.require("{os.path.join(module_path, "linux-perf.js")}").{action}()',
            "replMode": True,
        },
    }
    _send_socket_request(sock, cdp_request)


def _validate_ns_node(sock: WebSocket, expected_ns_link_name: str) -> None:
    command = 'const fs = process.mainModule.require("fs"); fs.readlinkSync("/proc/self/ns/pid")'
    actual_ns_link_name = cast(str, _execute_js_command(sock, command))
    assert (
        actual_ns_link_name == expected_ns_link_name
    ), f"Wrong namespace, expected {expected_ns_link_name}, got {actual_ns_link_name}"


def _validate_pid(expected_pid: int, sock: WebSocket) -> None:
    actual_pid = cast(int, _execute_js_command(sock, "process.pid"))
    assert expected_pid == actual_pid, f"Wrong pid, expected {expected_pid}, actual {actual_pid}"


def create_debugger_socket(nspid: int, ns_link_name: str) -> WebSocket:
    debugger_url = _get_debugger_url()
    sock = create_connection(debugger_url)
    sock.settimeout(10)
    _validate_ns_node(sock, ns_link_name)
    _validate_pid(nspid, sock)
    return sock


def _copy_module_into_process_ns(process: psutil.Process, musl: bool, version: str) -> str:
    proc_root = get_proc_root_path(process)
    libc = "musl" if musl else "glibc"
    dest_inside_container = _get_dest_inside_container(musl, version)
    dest = resolve_proc_root_links(proc_root, dest_inside_container)
    if os.path.exists(dest):
        return dest_inside_container
    src = resource_path(os.path.join("node", "module", libc, _get_dso_git_rev(), version))
    shutil.copytree(src, dest)
    add_permission_dir(dest, stat.S_IROTH, stat.S_IXOTH | stat.S_IROTH)
    return dest_inside_container


def _generate_perf_map(module_path: str, nspid: int, ns_link_name: str) -> None:
    sock = create_debugger_socket(nspid, ns_link_name)
    _change_dso_state(sock, module_path, "start")


def _clean_up(module_path: str, nspid: int, ns_link_name: str) -> None:
    sock = create_debugger_socket(nspid, ns_link_name)
    try:
        _change_dso_state(sock, module_path, "stop")
    finally:
        os.remove(os.path.join("/tmp", f"perf-{nspid}.map"))


def get_node_processes() -> List[psutil.Process]:
    return pgrep_exe(r".*node[^/]*$")


def generate_map_for_node_processes(processes: List[psutil.Process]) -> None:
    """Iterates over all NodeJS processes, starts debugger for it, finds debugger URL,
    copies node-linux-perf module into process' namespace, loads module and starts it."""
    for process in processes:
        try:
            musl = is_musl(process)
            node_major_version = _get_node_major_version(process)
            dest = _copy_module_into_process_ns(process, musl, node_major_version)
            nspid = get_process_nspid(process.pid)
            ns_link_name = os.readlink(f"/proc/{process.pid}/ns/pid")
            _start_debugger(process.pid)
            run_in_ns(
                ["pid", "mnt", "net"],
                lambda: _generate_perf_map(dest, nspid, ns_link_name),
                process.pid,
                passthrough_exception=True,
            )
        except Exception as e:
            logger.warning(f"Could not create debug symbols for pid {process.pid}. Reason: {e}", exc_info=True)


def clean_up_node_maps(processes: List[psutil.Process]) -> None:
    """Stops generating perf maps for each NodeJS process and cleans up generated maps"""
    for process in processes:
        try:
            if not is_process_running(process):
                continue
            node_major_version = _get_node_major_version(process)
            nspid = get_process_nspid(process.pid)
            ns_link_name = os.readlink(f"/proc/{process.pid}/ns/pid")
            dest = _get_dest_inside_container(is_musl(process), node_major_version)
            run_in_ns(
                ["pid", "mnt", "net"],
                lambda: _clean_up(dest, nspid, ns_link_name),
                process.pid,
                passthrough_exception=True,
            )
        except Exception as e:
            logger.warning(f"Could not clean up debug symbols for pid {process.pid}. Reason: {e}", exc_info=True)
