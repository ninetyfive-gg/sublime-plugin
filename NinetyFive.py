import http.client
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

    def connect(self):
        try:
            self._ws_app = websocket.WebSocketApp(
                self.url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=lambda ws: print("Connected to WebSocket server..."),
            )
            self._ws_app.run_forever()
        except Exception as e:
            print(f"Failed to connect: {e}")

    def _on_error(self, ws, error):
        print(f"Websocket Error: {error}")

    def _on_close(self, ws, close_status_code, message):
        print(f"Websocket closed with code {close_status_code} {message}")

    def _on_message(self, ws, message):
        global active_request_id, suggestion
        data = json.loads(message)
        if data.get("type") == "subscription-info":
            message = "Premium" if data["isPaid"] else "Free"
            sublime.active_window().active_view().run_command(
                "set_ninety_five_status", {"message": message}
            )
        if data.get("r") is not None:
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
        if self._ws_app:
            self._ws_app.close()


class SetNinetyFiveStatusCommand(sublime_plugin.TextCommand):
    def run(self, edit, message):
        self.view.set_status("ninetyfive-status", "NinetyFive: " + message)


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
            f"{endpoint}?user_id={user_id}&api_key={api_key}"
        )
        threading.Thread(target=websocket_instance.connect).start()

    def on_load_async(self, view):
        global active_commit

        try:
            cwd = view.window().folders()[0]
            result = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD", "HEAD"], cwd=cwd, text=True
            ).splitlines()

            branch, hash = result

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
