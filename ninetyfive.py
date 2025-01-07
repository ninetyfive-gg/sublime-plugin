import http.client
import json
import threading
import time
import urllib
import uuid

import sublime
import sublime_plugin
import websocket

# Since we're gonna use `plugin_unloaded` to close the connection, we need the ws handler available
websocket_instance = None

active_request_id = None
accumulated_completion = ""
suggestion = ""


def plugin_loaded():
    print("loaded")


def plugin_unloaded():
    global websocket_instance
    if websocket_instance:
        websocket_instance.close()
        websocket_instance = None


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
        global active_request_id, accumulated_completion, suggestion
        data = json.loads(message)
        if data.get("type") == "subscription-info":
            message = "Premium" if data["isPaid"] else "Free"
            sublime.active_window().active_view().run_command(
                "set_ninetyfive_status", {"message": message}
            )
        if data.get("v") is not None and data.get("r") is not None:
            if data["r"] == active_request_id:
                completion_fragment = data["v"]
                if completion_fragment is not None:
                    accumulated_completion = (
                        accumulated_completion + completion_fragment
                    )

                self._process_completion(completion_fragment)

    def _process_completion(self, completion_fragment):
        global active_request_id, accumulated_completion
        view = sublime.active_window().active_view()
        cursor_position = view.sel()[0].begin()
        text = view.substr(sublime.Region(0, cursor_position))

        # Ensure the completion exceeds the base text
        if len(accumulated_completion) <= len(text):
            return

        if completion_fragment is None:
            # TODO return completion
            return

        # Find the first non-whitespace character in the new completion text
        first_non_whitespace_index = next(
            (
                i
                for i, c in enumerate(accumulated_completion[len(text) :])
                if not c.isspace()
            ),
            -1,
        )
        if first_non_whitespace_index == -1:
            return
        first_non_whitespace_index += len(text)

        # Ensure the text completes the remainder of the line
        newline_index = accumulated_completion.find("\n", first_non_whitespace_index)
        if newline_index == -1:
            return

        # Verify that the next line's indentation is present
        last_line = accumulated_completion[newline_index + 1 :]
        second_non_whitespace_index = next(
            (i for i, c in enumerate(last_line) if not c.isspace()), -1
        )
        if second_non_whitespace_index == -1:
            return

        global suggestion
        suggestion = accumulated_completion[
            cursor_position : newline_index + second_non_whitespace_index + 1
        ]

        # Cancel the completion request
        self.send_message(
            json.dumps(
                {
                    "type": "cancel-completion-request",
                    "requestId": active_request_id,
                }
            )
        )

        # Check if criteria to show completion is met
        print("ready to trigger completion...", time.time())
        view.run_command(
            "trigger_ninetyfive_completion",
            {"message": "\n".join(suggestion)},
        )

        # Clear request
        active_request_id = None
        accumulated_completion = ""
        suggestion = ""

    def send_message(self, message: str):
        if self._ws_app and self._ws_app.sock and self._ws_app.sock.connected:
            try:
                self._ws_app.send(message)
            except Exception as e:
                print(f"Failed to send message: {e}")

    def close(self):
        print("try close")
        if self._ws_app:
            self._ws_app.close()


class SetNinetyfiveStatusCommand(sublime_plugin.TextCommand):
    def run(self, edit, message):
        self.view.set_status("ninetyfive-status", "Ninetfive: " + message)


class TriggerNinetyfiveCompletionCommand(sublime_plugin.TextCommand):
    def run(self, edit, message):
        self.view.run_command(
            "auto_complete",
            {
                "disable_auto_insert": True,
                "api_completions_only": False,
                "next_completion_if_showing": False,
            },
        )


class PurchaseNinetyfiveCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        self.view.window().run_command(
            "open_url", {"url": "https://ninetyfive.gg/api/payment"}
        )


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
        settings = sublime.load_settings("Ninetyfive.sublime-settings")
        settings.set("api_key", user_input)
        sublime.save_settings("Ninetyfive.sublime-settings")


class NinetyFiveListener(sublime_plugin.EventListener):
    def __init__(self):
        global websocket_instance
        websocket_instance = WebSocketHandler("ws://100.65.232.81:8000")
        threading.Thread(target=websocket_instance.connect).start()

    def on_modified(self, view):
        global active_request_id, websocket_instance
        # Get the text up to the cursor position
        cursor_position = view.sel()[0].begin()
        text_to_cursor = view.substr(sublime.Region(0, cursor_position))

        # Send the text to the WebSocket
        print("generating uuid...", time.time())
        active_request_id = str(uuid.uuid4())
        print("generated uuid...", time.time())
        websocket_instance.send_message(
            json.dumps(
                {
                    "requestId": active_request_id,
                    "type": "completion-request",
                    "prefix": text_to_cursor,
                    "suffix": "",
                    "path": "/fake/path",
                    "workspace": "test",
                }
            )
        )
        print("send completion-request...", time.time())

    def on_query_completions(self, view, prefix, locations):
        global suggestion
        print("on_query_completions", time.time())
        completions = [
            sublime.CompletionItem(
                accumulated_completion,
                annotation="NinetyFive",
                completion=accumulated_completion,
                completion_format=sublime.COMPLETION_FORMAT_TEXT,
                kind=(
                    sublime.KIND_ID_COLOR_CYANISH,
                    "attribution",
                    "some detail",
                ),
            )
        ]

        return sublime.CompletionList(
            completions=completions,
            flags=sublime.DYNAMIC_COMPLETIONS | sublime.INHIBIT_WORD_COMPLETIONS,
        )
