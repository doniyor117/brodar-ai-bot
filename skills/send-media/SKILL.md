---
name: send-media
description: How to send local files (images, audio, videos, documents) to a Telegram chat.
---

# Delivering Media and Files to a Chat

When the Master Admin or a user asks you to send them a file, image, video, audio, or document, you have a native tool you can use: `send_file`.

## How to use `send_file`
The `send_file` tool uploads a file from the server's local file system directly into the Telegram chat where you were asked.

### Parameters
- **file_path** (string): The *absolute path* to the local file (e.g., `/mnt/projects/brodar-ai-bot/workspace/report.pdf`).
- **file_type** (string): You must specify the type of file you are sending. Choose from:
  - `document` (For PDFs, text files, archives, code scripts)
  - `photo` (For images like JPG, PNG)
  - `video` (For MP4s, MOVs)
  - `audio` (For MP3s, WAVs)
- **caption** (string, optional): A text message to attach underneath the file.

### Important Notes
- **You can only send local files.** If the user asks for a file from the internet, you must first download it locally using `execute_shell_command` (e.g., `wget -O /tmp/file.jpg <url>`), and then use `send_file` with the path `/tmp/file.jpg`.
- **Generated Images:** If the user asks you to *generate* an image, do NOT use this tool. Use the `image_generate` tool instead. Use `send_file` only for existing files.
