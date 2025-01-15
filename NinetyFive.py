import http.client
import json
import re
import threading
import time
import urllib
import uuid

import sublime
import sublime_plugin
import websocket

# Since we're gonna use `plugin_unloaded` to close the connection, we need the ws handler available
websocket_instance = None
payment_id = None

active_request_id = None
accumulated_completion = ""
suggestion = ""


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
                "set_ninetyfive_status", {"message": message}
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
            elif len(accumulated_completion) > 0 and completion_fragment == "\n":
                suggestion = accumulated_completion
                view.run_command(
                    "trigger_ninetyfive_completion",
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
            view.run_command(
                "trigger_ninetyfive_completion",
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


class SetNinetyfiveStatusCommand(sublime_plugin.TextCommand):
    def run(self, edit, message):
        self.view.set_status("ninetyfive-status", "Ninetyfive: " + message)


class TriggerNinetyfiveCompletionCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.run_command(
            "auto_complete",
            {
                "disable_auto_insert": True,
                "api_completions_only": True,
                "next_completion_if_showing": False,
            },
        )


class PurchaseNinetyfiveCommand(sublime_plugin.TextCommand):
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
        base_url = "ninetyfive.gg"

        while time.time() - start_time < timeout:
            try:
                conn = http.client.HTTPSConnection(base_url)
                conn.request("GET", f"/api/keys/{payment_id}")
                response = conn.getresponse()

                if response.status == 200:
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

                conn.close()
                time.sleep(10)

            except Exception as e:
                print(f"Error polling for API key: {e}")
                time.sleep(2)


class SendNinetyfiveKeyCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.window().show_input_panel(
            "Enter a your email:", "", self.on_done, None, None
        )

    def on_done(self, user_input):
        base_url = "ninetyfive.gg"
        endpoint = "/api/resend"
        query_params = {"email": user_input}
        encoded_params = urllib.parse.urlencode(query_params)
        conn = http.client.HTTPSConnection(base_url)
        headers = {"Content-Type": "application/json"}
        conn.request("POST", f"{endpoint}?{encoded_params}", {}, headers)
        response = conn.getresponse()
        if response.status == 204:
            sublime.message_dialog("Email sent!")
        else:
            sublime.message_dialog(
                "Failed to send email. Contact help@ninetyfive.gg for assistance."
            )

        conn.close()


class SetNinetyfiveKeyCommand(sublime_plugin.TextCommand):
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
        endpoint = settings.get("server_endpoint", "ws://100.118.7.128:8000")
        websocket_instance = WebSocketHandler(endpoint)
        threading.Thread(target=websocket_instance.connect).start()

    def on_modified(self, view):
        global active_request_id, websocket_instance, accumulated_completion, suggestion
        accumulated_completion = ""
        suggestion = ""

        # We restrict to the "code windows"
        if (
            not view.settings().get("is_widget")
            and view.window()
            and view.window().active_view() == view
        ):
            # Get the text up to the cursor position
            cursor_position = view.sel()[0].begin()
            text_to_cursor = view.substr(sublime.Region(0, cursor_position))
            text_after_cursor = view.substr(
                sublime.Region(cursor_position, view.size())
            )

        sel = view.sel()[0]
        position = sel.begin()

        # Get prefix
        prefix = None
        bos = False
        for i in range(view.rowcol(position)[0]):
            region = sublime.Region(view.text_point(i, 0), position)
            text = view.substr(region)
            if len(text) >= 4096:
                continue
            prefix = text
            bos = i == 0
            break

        if not prefix:
            region = sublime.Region(
                view.text_point(view.rowcol(position)[0], 0), position
            )
            prefix = view.substr(region)
            bos = view.rowcol(position)[0] == 0

        if len(prefix) > 4096:
            prefix = prefix[-4096:]

        # Get suffix
        suffix = None
        eos = False
        for i in range(view.rowcol(position)[0], view.rowcol(view.size())[0] + 1):
            region = sublime.Region(
                position, view.text_point(i, view.rowcol(view.size())[1])
            )
            text = view.substr(region)
            if len(text) >= 2048:
                break
            suffix = text
            eos = i == view.rowcol(view.size())[0]

        if not suffix:
            line_end = view.line(position).end()
            region = sublime.Region(position, line_end)
            suffix = view.substr(region)
            eos = view.rowcol(position)[0] == view.rowcol(view.size())[0]

        if len(suffix) > 2048:
            suffix = suffix[:2048]

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
        directory = view.window().folders()[0]
        print("send", prefix)
        websocket_instance.send_message(
            json.dumps(
                {
                    "requestId": active_request_id,
                    "type": "completion-request",
                    "prefix": prefix,
                    "suffix": suffix,
                    "path": view.file_name(),
                    "repo": directory if directory else "unknown",
                    "folderId": view.file_name(),
                    "eos": eos,
                    "bos": False,
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
