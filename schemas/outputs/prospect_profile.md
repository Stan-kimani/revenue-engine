{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "prospect_profile",
  "description": "Synthesis of company + contact enrichment into an actionable profile. personalization_anchors are the ONLY facts downstream outreach may assert about this prospect.",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "summary",
    "likely_challenges",
    "personalization_anchors",
    "disqualifying_signals",
    "recommended_angle",
    "insufficient_context"
  ],
  "properties": {
    "summary": {
      "type": "string",
      "maxLength": 600,
      "description": "Two to three sentences. What this business is and where friction plausibly lives."
    },
    "likely_challenges": {
      "type": "array",
      "maxItems": 5,
      "items": {
        "type": "object",
        "required": [
          "challenge",
          "basis",
          "confidence"
        ],
        "additionalProperties": false,
        "properties": {
          "challenge": {
            "type": "string",
            "maxLength": 200
          },
          "basis": {
            "type": "string",
            "maxLength": 300
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      }
    },
    "personalization_anchors": {
      "type": "array",
      "maxItems": 6,
      "description": "Verifiable specifics drawn from context. Each has a stable id referenced by outreach drafts. If none can be grounded, return an empty array.",
      "items": {
        "type": "object",
        "required": [
          "anchor_id",
          "fact",
          "source",
          "confidence"
        ],
        "additionalProperties": false,
        "properties": {
          "anchor_id": {
            "type": "string",
            "pattern": "^anchor_[0-9]{1,2}$"
          },
          "fact": {
            "type": "string",
            "maxLength": 240,
            "description": "A specific, checkable statement. Not an inference."
          },
          "source": {
            "type": "string",
            "maxLength": 300,
            "description": "Where in the provided context this came from (url or field name)."
          },
          "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1
          }
        }
      }
    },
    "disqualifying_signals": {
      "type": "array",
      "maxItems": 5,
      "items": {
        "type": "object",
        "required": [
          "signal",
          "evidence"
        ],
        "additionalProperties": false,
        "properties": {
          "signal": {
            "type": "string",
            "maxLength": 160
          },
          "evidence": {
            "type": "string",
            "maxLength": 300
          }
        }
      }
    },
    "recommended_angle": {
      "type": [
        "string",
        "null"
      ],
      "maxLength": 400
    },
    "insufficient_context": {
      "type": "boolean"
    }
  }
}
