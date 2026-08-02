---
name: web_scraper
title: 🌐 Web Scraper & Crawler
description: Extract data, structured tables, articles, metadata, and JSON endpoints from websites using requests and BeautifulSoup4 with clean error handling and user-agent rotation.
keywords: [scrape, scraper, crawl, crawler, beautifulsoup, bs4, requests, html parse, web scraping, extract data, استخراج داده, خزش]
packages: [requests, beautifulsoup4]
---

# Web Scraper Skill

Extracts text, HTML tables, news, prices, or links and saves structured JSON/CSV files.

## Core Guidelines & Best Practices

1. **Robust Headers & User-Agents**:
   - Always specify realistic browser User-Agent headers to prevent 403 Forbidden blocks.
   ```python
   import requests
   from bs4 import BeautifulSoup
   import json

   headers = {
       "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
       "Accept-Language": "en-US,en;q=0.9",
   }
   ```
2. **Resilient Parsing**:
   - Handle network timeouts and missing HTML elements gracefully.
   ```python
   resp = requests.get("https://news.ycombinator.com", headers=headers, timeout=15)
   resp.raise_for_status()
   soup = BeautifulSoup(resp.text, "html.parser")
   items = []
   for tag in soup.select(".titleline > a")[:10]:
       items.append({"title": tag.get_text(strip=True), "url": tag.get("href")})

   with open("scraped_data.json", "w", encoding="utf-8") as f:
       json.dump(items, f, indent=2, ensure_ascii=False)
   ```
