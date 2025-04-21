import base64
import gzip
import io
import json
import os
import subprocess
import threading
import time
import uuid

import requests
import sublime
import sublime_plugin
import websocket

from .git import parse_numstat, parse_tree

# Since we're gonna use `plugin_unloaded` to close the connection, we need the ws handler available
websocket_instance = None
payment_id = None

active_request_id = None
accumulated_completion = ""
suggestion = ""
active_commit = None


def plugin_unloaded():
    global websocket_instance
    if websocket_instance:
        websocket_instance.close()
        websocket_instance = None


def starts_with_whitespace(text):
    return text[0].isspace() if text else False


# Websocket client
class WebSocketHandler:
    def __init__(self, url):
        self.url = url
        websocket.enableTrace(False)
        self._ws_app = None
        self._reconnect = True
        self._reconnect_delay = 1  # Delay in seconds

    def connect(self):
        while self._reconnect:
            try:
                self._ws_app = websocket.WebSocketApp(
                    self.url,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self._ws_app.run_forever()
            except Exception as e:
                print(f"Failed to connect: {e}")

    def _on_open(self, ws):
        print("Connected to WebSocket server...")
        self._reconnect_delay = 1  # Back to the lowest delay

    def _on_error(self, ws, error):
        print(f"Websocket Error: {error}")
        if self._reconnect:
            self._reconnect_delay = min(
                self._reconnect_delay * 2, 32
            )  # 32s as a max is ok?

    def _on_close(self, ws, close_status_code, message):
        print(f"Websocket closed with code {close_status_code} {message}")
        if self._reconnect:
            print(f"Attempting to reconnect in {self._reconnect_delay} seconds...")
            time.sleep(self._reconnect_delay)

    def _on_message(self, ws, message):
        global active_request_id, suggestion
        data = json.loads(message)

        data_type = data.get("type")

        if data_type == "subscription-info":
            message = data["name"]
            sublime.active_window().active_view().run_command(
                "set_ninety_five_status", {"message": message}
            )
        elif data_type == "get-commit":
            hash = data.get("commitHash")

            cwd = sublime.active_window().folders()[0]

            if cwd is None or cwd == "":
                return

            try:
                raw_numstat = subprocess.check_output(
                    ["git", "show", "--numstat", hash], cwd=cwd, text=True
                ).strip()

                numstat = parse_numstat(raw_numstat)

                commit = subprocess.check_output(
                    ["git", "log", "-s", "--format=%P%n%B", hash], cwd=cwd, text=True
                ).strip()

                parents_line, message = commit.split("\n", 1)
                parents = parents_line.split()

                raw_tree = subprocess.check_output(
                    ["git", "ls-tree", "-r", "-l", "--full-tree", hash],
                    cwd=cwd,
                    text=True,
                ).strip()

                tree = parse_tree(raw_tree)

                files = [
                    {
                        **file,
                        **next((lsf for lsf in tree if lsf["file"] == file["to"]), {}),
                    }
                    for file in numstat
                    if file["additions"] is not None and file["deletions"] is not None
                ]

                message = {"parents": parents, "message": message, "files": files}

                if websocket_instance:
                    websocket_instance.send_message(
                        json.dumps(
                            {
                                "type": "commit",
                                "commitHash": hash,
                                "commit": message,
                            }
                        )
                    )
            except Exception as e:
                print("failed to send commit", e)

        elif data_type == "get-blob":
            hash = data.get("commitHash")
            path = data.get("path")
            object_hash = data.get("objectHash")
            cwd = sublime.active_window().folders()[0]

            if cwd is None or cwd == "":
                return

            try:
                blob = subprocess.check_output(
                    ["git", "show", f"{hash}:{path}"], cwd=cwd
                )

                diff = subprocess.check_output(
                    ["git", "diff", f"{hash}^", hash, "--", path],
                    cwd=cwd,
                )

                compressed_blob = io.BytesIO()
                with gzip.GzipFile(fileobj=compressed_blob, mode="wb") as f:
                    f.write(blob)
                encoded_blob = base64.b64encode(compressed_blob.getvalue()).decode(
                    "utf-8"
                )

                compressed_diff = io.BytesIO()
                with gzip.GzipFile(fileobj=compressed_diff, mode="wb") as f:
                    f.write(diff)
                encoded_diff = base64.b64encode(compressed_diff.getvalue()).decode(
                    "utf-8"
                )

                if websocket_instance:
                    websocket_instance.send_message(
                        json.dumps(
                            {
                                "type": "blob",
                                "commitHash": hash,
                                "objectHash": object_hash,
                                "path": path,
                                "blob": encoded_blob,
                                "diff": encoded_diff,
                            }
                        )
                    )
            except Exception as e:
                print("failed to send blob", e)

        elif data.get("r") is not None:
            if data["r"] == active_request_id:
                completion_fragment = data["v"]
                if isinstance(completion_fragment, str):
                    for char in completion_fragment:
                        self._process_completion(char)
                else:
                    self._process_completion(completion_fragment)

    def _process_completion(self, completion_fragment):
        global active_request_id, accumulated_completion, suggestion
        view = sublime.active_window().active_view()

        if completion_fragment is not None:
            print(
                accumulated_completion.encode("utf-8"),
                completion_fragment.encode("utf-8"),
            )
            if len(accumulated_completion) == 0 and completion_fragment is None:
                self.send_message(
                    json.dumps(
                        {
                            "type": "cancel-completion-request",
                            "requestId": active_request_id,
                        }
                    )
                )
                return
            elif len(accumulated_completion) > 0 and "\n" in completion_fragment:
                accumulated_completion += completion_fragment[
                    : completion_fragment.index("\n") + 1
                ]
                suggestion = accumulated_completion
                if len(suggestion.strip()) > 0:
                    view.run_command(
                        "trigger_ninety_five_completion",
                    )

                # Clear state
                active_request_id = None
                accumulated_completion = ""
                return
            else:
                accumulated_completion += completion_fragment
                return

        if completion_fragment is None:
            # EOF reached, trigger completion with accumulated text
            suggestion = accumulated_completion
            if len(suggestion.strip()) > 0:
                view.run_command(
                    "trigger_ninety_five_completion",
                )

            # Clear state
            active_request_id = None
            accumulated_completion = ""
            return

        return

    def send_message(self, message: str):
        if self._ws_app and self._ws_app.sock and self._ws_app.sock.connected:
            try:
                self._ws_app.send(message)
            except Exception as e:
                print(f"Failed to send message: {e}")

    def close(self):
        self._reconnect = False
        if self._ws_app:
            self._ws_app.close()


class SetNinetyFiveStatusCommand(sublime_plugin.TextCommand):
    def run(self, edit, message):
        self.view.set_status("ninetyfive-status", message)


class TriggerNinetyFiveCompletionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.run_command(
            "auto_complete",
            {
                "disable_auto_insert": True,
                "api_completions_only": True,
                "next_completion_if_showing": False,
            },
        )


class PurchaseNinetyFiveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        global payment_id, websocket_instance
        payment_id = str(uuid.uuid4())
        client_identifier = "sublime_" + payment_id
        self.view.window().run_command(
            "open_url",
            {
                "url": "https://ninetyfive.gg/api/payment?client_reference_id="
                + client_identifier
            },
        )

        threading.Thread(target=self.poll_for_api_key).start()

    def poll_for_api_key(self):
        global payment_id, websocket_instance
        if not payment_id:
            return

        start_time = time.time()
        timeout = 120
        base_url = f"https://ninetyfive.gg/api/keys/{payment_id}"

        while time.time() - start_time < timeout:
            try:
                response = requests.get(base_url)

                if response.status_code == 200:
                    data = json.loads(response.read().decode())
                    if data.get("api_key"):
                        settings = sublime.load_settings("NinetyFive.sublime-settings")
                        settings.set("api_key", data["api_key"])
                        sublime.save_settings("NinetyFive.sublime-settings")

                        if websocket_instance:
                            websocket_instance.send_message(
                                json.dumps(
                                    {
                                        "type": "set-api-key",
                                        "key": data["api_key"],
                                    }
                                )
                            )
                        return

                time.sleep(10)

            except Exception as e:
                print(f"Error polling for API key: {e}")
                time.sleep(2)


class SendNinetyFiveKeyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.window().show_input_panel(
            "Enter a your email:", "", self.on_done, None, None
        )

    def on_done(self, user_input):
        url = "https://ninetyfive.gg/api/resend"
        params = {"email": user_input}
        response = requests.post(url, params=params)
        if response.status_code == 204:
            sublime.message_dialog("Email sent!")
        else:
            sublime.message_dialog(
                "Failed to send email. Contact help@ninetyfive.gg for assistance."
            )


class SetNinetyFiveKeyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.window().show_input_panel(
            "Enter a your API Key:", "", self.on_done, None, None
        )

    def on_done(self, user_input):
        global websocket_instance
        self.store_string(user_input)

        websocket_instance.send_message(
            json.dumps(
                {
                    "type": "set-api-key",
                    "key": user_input,
                }
            )
        )

    def store_string(self, user_input):
        settings = sublime.load_settings("NinetyFive.sublime-settings")
        settings.set("api_key", user_input)
        sublime.save_settings("NinetyFive.sublime-settings")


class NinetyFiveListener(sublime_plugin.EventListener):
    def __init__(self):
        global websocket_instance

        settings = sublime.load_settings("NinetyFive.sublime-settings")
        endpoint = settings.get("server_endpoint", "wss://api.ninetyfive.gg")
        user_id = settings.get("user_id", str(uuid.uuid4()))
        api_key = settings.get("api_key", "")
        settings.set("user_id", user_id)

        websocket_instance = WebSocketHandler(
            f"{endpoint}?user_id={user_id}&api_key={api_key}&editor=sublime"
        )
        threading.Thread(target=websocket_instance.connect).start()

    def on_load_async(self, view):
        global active_commit

        try:
            cwd = view.window().folders()[0]
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, text=True
            ).strip()

            hash = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=cwd, text=True
            ).strip()

            if hash != active_commit:
                active_commit = hash
                if hash and branch:
                    project = os.path.basename(view.window().folders()[0])
                    websocket_instance.send_message(
                        json.dumps(
                            {
                                "type": "set-workspace",
                                "commitHash": hash,
                                "path": view.window().folders()[0],
                                "name": f"{project}/{branch}",
                            }
                        )
                    )
                else:
                    websocket_instance.send_message(
                        json.dumps({"type": "set-workspace"})
                    )
        except Exception as e:
            print("failed setting workspace", e)

    def on_modified(self, view):
        global active_request_id, websocket_instance, accumulated_completion, suggestion
        accumulated_completion = ""
        suggestion = ""

        # We restrict to the "code windows"
        if (
            view.settings().get("is_widget")
            or not view.window()
            or view.window().active_view() != view
        ):
            return

        cwd = sublime.active_window().folders()[0]

        if cwd is None or cwd == "":
            return

        try:
            print("about to check ignore", view.file_name())
            raw_ignore = subprocess.check_output(
                ["git", "check-ignore", view.file_name()], cwd=cwd, text=True
            ).strip()

            # Do not leak ignored files
            if len(raw_ignore) > 0:
                print("file is ignored")
                return
        except subprocess.CalledProcessError as e:
            if e.returncode == 1:
                # Exit code 1 means its not ignored
                pass
            else:
                raise
        except Exception as e:
            print("failed to check-ignore", e)

        # Send the file
        websocket_instance.send_message(
            json.dumps(
                {
                    "type": "file-content",
                    "path": view.window().folders()[0],
                    "text": view.substr(sublime.Region(0, view.size())),
                }
            )
        )

        # Cancel everything else!
        if active_request_id:
            websocket_instance.send_message(
                json.dumps(
                    {
                        "type": "cancel-completion-request",
                        "requestId": active_request_id,
                    }
                )
            )

        active_request_id = str(uuid.uuid4())
        if view.window():
            directory = view.window().folders()[0]

        text = view.substr(sublime.Region(0, view.sel()[0].begin()))
        pos = len(text.encode("utf-8"))
        websocket_instance.send_message(
            json.dumps(
                {
                    "requestId": active_request_id,
                    "type": "delta-completion-request",
                    "repo": directory if directory else "unknown",
                    "pos": pos,
                }
            )
        )

    def on_query_completions(self, view, prefix, locations):
        global suggestion, active_request_id
        if not suggestion:
            return None

        print("suggestion", suggestion)
        completions = [
            sublime.CompletionItem(
                suggestion,
                annotation="NinetyFive",
                completion=suggestion,
                completion_format=sublime.COMPLETION_FORMAT_TEXT,
                kind=(
                    sublime.KIND_ID_COLOR_CYANISH,
                    "attribution",
                    "some detail",
                ),
            )
        ]

        # Clear the suggestion after creating completion item
        suggestion = ""

        return sublime.CompletionList(
            completions,
            flags=sublime.DYNAMIC_COMPLETIONS | sublime.INHIBIT_WORD_COMPLETIONS,
        )
