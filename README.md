# Zulip Moderation AI Bot
AI-powered content moderation for Zulip — MLOps course project.

**Team:** Aadarsh LN (al9581) · Nitish KS (nk4277) · Purvansh Arora (pa2757) · Rishabh Narayan (rn2718)

---

## Repo Structure
```
src/
```

---

## Setup

```bash
pip install -r requirements.txt
```

TODO: Decide - Use whatever environment manager your team prefers (venv, conda, etc.).

---

## Running Locally

```bash
uvicorn src.main:app --host 0.0.0.0 --port 8000 - TODO: confirm
```

---

## Running with Docker

```bash
```

---

## API

### `POST /moderate`

**Request**
```json

```

**Response**
```json

```

**Decision tiers**

| Action | Condition |
|---|---|
| `ALLOW` | toxicity < 0.60 and self_harm < 0.30 |
| `WARN_AND_OBSCURE` | toxicity 0.60–0.85 |
| `HIDE_AND_STRIKE` | toxicity > 0.85 |
| `ALERT_ADMIN` | self_harm > 0.30 (message not removed) |
