{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "objection_response",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "objection_category",
    "approach",
    "body",
    "precedents_used",
    "requires_human",
    "confidence"
  ],
  "properties": {
    "objection_category": {
      "enum": [
        "price",
        "timing",
        "trust",
        "need",
        "authority",
        "competitor",
        "other"
      ]
    },
    "approach": {
      "enum": [
        "acknowledge_and_reframe",
        "provide_proof",
        "narrow_scope",
        "defer_with_date",
        "qualify_out",
        "hand_to_human"
      ]
    },
    "body": {
      "type": "string",
      "minLength": 30,
      "maxLength": 1000
    },
    "precedents_used": {
      "type": "array",
      "maxItems": 3,
      "items": {
        "type": "string",
        "maxLength": 120,
        "description": "Retrieved memory ids of past objection handling. Never fabricated."
      }
    },
    "requires_human": {
      "type": "boolean",
      "description": "true for any pricing commitment, discount, contract or legal content."
    },
    "confidence": {
      "type": "number",
      "minimum": 0,
      "maximum": 1
    }
  }
}
