from openai import OpenAI

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = "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"
)

completion = client.chat.completions.create(
  model="mistralai/mistral-nemotron",
  messages=[{"role":"user","content":""}],
  temperature=0.6,
  top_p=0.7,
  max_tokens=4096,
  stream=True
)

for chunk in completion:
  if chunk.choices and chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")

