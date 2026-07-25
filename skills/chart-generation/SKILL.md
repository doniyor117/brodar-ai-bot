---
name: chart-generation
description: Generates graphs and charts using QuickChart.io
---
# Chart Generation via QuickChart

You have the ability to generate graphs and charts instantly without needing external tools.
When a user asks to visualize data (e.g. a bar chart, pie chart, line graph), use QuickChart.io.

## Format
Construct a JSON configuration of the chart based on the Chart.js specification, and embed it as a markdown image link:
`![Chart title](https://quickchart.io/chart?c=URL_ENCODED_JSON)`

## Instructions
1. Create a Chart.js JSON configuration object containing the `type`, `data`, and `options`.
2. Compact the JSON into a single string without spaces (e.g., `{type:'bar',data:{labels:['Q1','Q2'],datasets:[{data:[50,60]}]}}`).
3. URL-encode the JSON string.
4. Construct the markdown image. The image will be rendered directly in Telegram.

## Example
User: "Make a pie chart showing 40% apples, 60% oranges"
You: `Here is the chart you requested: ![Apples vs Oranges](https://quickchart.io/chart?c=%7Btype%3A'pie'%2Cdata%3A%7Blabels%3A%5B'Apples'%2C'Oranges'%5D%2Cdatasets%3A%5B%7Bdata%3A%5B40%2C60%5D%7D%5D%7D%7D)`
