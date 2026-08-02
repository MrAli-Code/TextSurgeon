---
name: fastapi_backend
title: ⚡ FastAPI Web Backend & REST API
description: Build high-performance REST APIs and web services using FastAPI and Pydantic with automated interactive documentation (/docs) and CORS support.
keywords: [fastapi, api, rest api, uvicorn, pydantic, backend, web server, endpoint, وب سرویس, بک‌اند]
packages: [fastapi, uvicorn]
---

# FastAPI Backend Skill

Builds REST API backends with type hints, schemas, and interactive Swagger docs.

## Core Guidelines & Best Practices

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional

app = FastAPI(title="Text Surgeon Agent API", version="1.0.0")

class Item(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    price: float

items_db: List[Item] = []

@app.get("/")
def read_root():
    return {"message": "Server running", "docs": "/docs"}

@app.get("/items", response_model=List[Item])
def get_items():
    return items_db

@app.post("/items", response_model=Item, status_code=201)
def create_item(item: Item):
    items_db.append(item)
    return item

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```
