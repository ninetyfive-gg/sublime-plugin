# NinetyFive Sublime Text Plugin
NinetyFive is a simple code completion extension.

- Code completions.
- (Work in progress) Training based on your projects to improve completions.

## Useful Info

NinetyFive sends your code to our servers whenever you perform modifications to the code in your open text editors, this code is then used to suggest completions based on the file content and your current position.

NinetyFive works by training a custom model on your codebase. Combined with an optimized inference engine, NinetyFive is able to provide lightning-fast autocomplete suggestions faster and with more context than GitHub Copilot and Cursor.

All code sent to NinetyFive is subject to a 14-day retention policy and is not used to train any models except your own.

## Development
1. Install Package Control on Sublime Text. Required to source dependencies.
2. Get to your Sublime Text packages directory:
- Windows: `%APPDATA%\Sublime Text\Packages`
- macOS: `~/Library/Application Support/Sublime Text/Packages`
- Linux: `~/.config/sublime-text/Packages`
3. Clone the repository: `git clone https://github.com/ninetyfive-gg/sublime-plugin.git ninetyfive`