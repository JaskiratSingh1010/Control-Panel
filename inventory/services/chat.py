import requests
from django.conf import settings


def chat(division, message, context):
    message = (message or '').strip()
    context = context or ''
    if not message:
        return {'reply': 'Please ask a question.'}

    if not settings.GROQ_API_KEY:
        return {'reply': 'AI chat not configured. Set GROQ_API_KEY in .env to enable.'}

    label = 'Jivo Oil' if division == 'oils' else 'Jivo Beverages'
    try:
        response = requests.post(
            'https://api.groq.com/openai/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {settings.GROQ_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': 'llama-3.3-70b-versatile',
                'max_tokens': 1024,
                'messages': [
                    {
                        'role': 'system',
                        'content': f'Inventory analyst for {label}. Context:\n{context}\nBe concise. Indian format (Cr/L).',
                    },
                    {'role': 'user', 'content': message},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()
        choices = data.get('choices', [])
        reply = choices[0].get('message', {}).get('content') if choices else data.get('error', {}).get('message', 'Error')
        if not reply:
            reply = 'No response from AI.'
        return {'reply': reply}
    except requests.HTTPError as e:
        try:
            data = response.json()
            return {'reply': data.get('error', {}).get('message') or str(e)}
        except Exception:
            return {'reply': str(e)}
    except Exception as e:
        return {'reply': str(e)}
