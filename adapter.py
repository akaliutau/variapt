import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
 
client = OpenAI(
    base_url="https://api.mubit.ai/openai/v1",
    api_key=os.environ["MUBIT_API_KEY"],
)
 
instructions = (
    "Sort the support message into one category: "
    "billing, bug, account or other. Reply with the category only."
)
templates = [
    "I was charged twice for my {} plan this month.",
    "The {} dashboard shows an error when I export a report.",
    "How do I change the email address on my {} account?",
    "Can I get a refund for the {} plan I cancelled yesterday?",
    "The {} mobile app closes when I open settings.",
    "I can't reset the password for my {} account.",
    "Do you offer the {} plan to charities?",
    "Where can I download last month's invoice for the {} plan?",
]
plans = ["Starter", "Team", "Business", "Enterprise", "Education"]
 
# 8 messages x 5 plans: 40 calls of one task.
for template in templates:
    for plan in plans:
        response = client.chat.completions.create(
            model="gpt-6-luna",
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": template.format(plan)},
            ],
        )
        print(response.choices[0].message.content)
