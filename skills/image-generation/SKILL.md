---
name: image-generation
description: Generates AI images instantly using Pollinations.ai (No API key required)
---
# Image Generation via Pollinations.ai

You have the ability to generate images instantly for the user without any python tools.
When a user asks for an image, simply construct a markdown image link in your response.
The image will be automatically rendered in Telegram.

## Format
`![Description of image](https://image.pollinations.ai/prompt/URL_ENCODED_PROMPT?width=1024&height=1024&nologo=true)`

## Instructions
1. Replace `URL_ENCODED_PROMPT` with the actual prompt. You MUST URL-encode the prompt (replace spaces with `%20` or `+`, etc.).
2. You can tweak the `width` and `height` parameters if the user wants a specific aspect ratio (e.g. 1920x1080).
3. Do not mention that you are generating the image using a URL. Just provide the image link and maybe a brief comment like "Here is the image you requested!".

## Example
User: "Show me a futuristic city at sunset"
You: `Here is your futuristic city! ![Futuristic city at sunset](https://image.pollinations.ai/prompt/futuristic%20city%20at%20sunset?width=1024&height=1024&nologo=true)`
