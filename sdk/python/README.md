# sphere

OpenAI-compatible client for the SPHERE metered AI gateway. Zero dependencies.

```python
from sphere import Client

c = Client(api_key="sphere_sk_...")          # or env SPHERE_API_KEY
r = c.chat.completions.create(
    model="anthropic/claude-3-haiku",
    messages=[{"role": "user", "content": "Hi"}],
)
print(r.choices[0].message.content)
print(c.balance())                            # {'balance_usd': 9.83, ...}
```

Typed errors: `InsufficientCreditsError` (402), `InvalidKeyError` (401),
`ModelNotFoundError` (404), `InvalidRequestError` (400), `APIError` (5xx).

v1 surface: `chat.completions.create` (non-streaming), `models.list`,
`balance()`. Streaming and `embeddings` are deferred (the gateway rejects
`stream=True` server-side; the client raises immediately).
