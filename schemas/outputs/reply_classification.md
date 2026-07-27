{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "reply_classification",
  "description": "Intent classification for an inbound reply. Confidence below config threshold routes to human review; the system never acts on a low-confidence guess.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "intent",
    "confidence",
    "reasoning",
    "objection_category",
    "escalation_signals",
    "suggested_action"
  ],
  "properties": {
    "intent": {
      "enum": [
        "interested",
        "objection",
        "not_now",
        "unsubscribe",
        "out_of_office",
        "referral",
        "unclear"
      ]
    },
    "confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1
    },
    "reasoning": {
      "type": "string",
      "maxLength": 600
    },
    "objection_category": {
      "enum": [
        "price",
        "timing",
        "trust",
        "need",
        "authority",
        "competitor",
        "other",
        null
      ],
      "description": "Closed vocabulary. null unless intent is objection."
    },
    "escalation_signals": {
      "type": "array",
      "maxItems": 6,
      "items": {
        "enum": [
          "anger",
          "complaint",
          "legal",
          "procurement",
          "pricing_question",
          "competitor_mention",
          "compliance",
          "enterprise_process"
        ]
      },
      "description": "Any signal here forces human escalation regardless of intent."
    },
    "suggested_action": {
      "enum": [
        "send_followup",
        "book_meeting",
        "pause_sequence",
        "escalate_human",
        "close_lost",
        "suppress_contact"
      ]
    },
    "referral_contact_named": {
      "type": [
        "string",
        "null"
      ],
      "maxLength": 160,
      "description": "Only when intent is referral. Never an invented address."
    },
    "requested_resume_date": {
      "type": [
        "string",
        "null"
      ],
      "format": "date",
      "description": "Only when intent is not_now and a date is explicitly stated."
    }
  }
}
