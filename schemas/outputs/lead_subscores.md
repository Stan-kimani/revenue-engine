{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "lead_subscores",
  "description": "Fuzzy scoring components ONLY. This output contains no total and no band; those are computed in code from config weights. additionalProperties:false prevents the model from returning either.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "buying_intent",
    "seniority_fit",
    "narrative_fit",
    "overall_note"
  ],
  "properties": {
    "buying_intent": {
      "type": "object",
      "required": [
        "score",
        "evidence",
        "confidence"
      ],
      "additionalProperties": false,
      "properties": {
        "score": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "description": "0 = no signal of active need. 1 = explicit stated need or active search."
        },
        "evidence": {
          "type": "array",
          "maxItems": 4,
          "items": {
            "type": "string",
            "maxLength": 300
          },
          "description": "Snippets from context. Empty array requires score <= 0.2."
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        }
      }
    },
    "seniority_fit": {
      "type": "object",
      "required": [
        "score",
        "evidence",
        "confidence"
      ],
      "additionalProperties": false,
      "properties": {
        "score": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "description": "Can this person authorise or champion a purchase of this kind."
        },
        "evidence": {
          "type": "array",
          "maxItems": 4,
          "items": {
            "type": "string",
            "maxLength": 300
          }
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        }
      }
    },
    "narrative_fit": {
      "type": "object",
      "required": [
        "score",
        "evidence",
        "confidence"
      ],
      "additionalProperties": false,
      "properties": {
        "score": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "description": "How well this business matches the ICP story beyond firmographic filters."
        },
        "evidence": {
          "type": "array",
          "maxItems": 4,
          "items": {
            "type": "string",
            "maxLength": 300
          }
        },
        "confidence": {
          "type": "number",
          "minimum": 0,
          "maximum": 1
        }
      }
    },
    "overall_note": {
      "type": "string",
      "maxLength": 400,
      "description": "One short paragraph a human can read to understand the judgment."
    }
  }
}
