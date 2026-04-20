# ChatSentry — System Flow

```
Zulip message sent
       │
       ▼
Data API (Rishabh)               ← POST /messages from Zulip webhook
  clean text
  persist to PostgreSQL/MinIO
       │
       │ POST /moderate (cleaned_text)
       ▼
Serving API (Purvansh)           ← hateBERT inference → decision
  ALLOW / WARN / HIDE / ALERT
       │
       │ Zulip API call
       ▼
Zulip (action taken)
  delete message / send DM warning / alert admin
       │
       │ admin decisions + user flags (async)
       ▼
Training Pipeline (Aadarsh)      ← retrains on new verified data
       │
       ▼
MLflow artifact → Serving API loads new model
```

## Open handoffs

| From | To | Status |
|---|---|---|
| Zulip webhook | Rishabh's data API | Nitish wires this up in K8s |
| Rishabh's data API | Purvansh's serving API | Rishabh needs to add POST /moderate call after cleaning |
| Purvansh's serving API | Zulip | **Missing — Purvansh's next piece** |
| Aadarsh's MLflow artifact | Purvansh's serving API | Not wired — serving still pulls from HuggingFace |
