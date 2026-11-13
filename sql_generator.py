
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)


def generate_sql(user_question, database_schema):

    system_prompt = """
    You are an expert PostgreSQL SQL developer.

    Your responsibility is to convert
    natural language into SQL queries.

    Follow these rules:

    1. Generate PostgreSQL-compatible SQL.
    2. Use only tables in the given schema.
    3. Use only existing columns.
    4. Use correct JOIN conditions.
    5. Generate SELECT queries only.
    6. Never generate INSERT, UPDATE or DELETE.
    7. Never invent table names.
    8. Return only the SQL query.
    """

    user_prompt = f"""
    DATABASE SCHEMA:

    {database_schema}

    USER QUESTION:

    {user_question}

    Generate the SQL query.
    """

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        temperature=0,
        messages=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ]
    )

    sql_query = response.choices[0].message.content

    return sql_query.strip()


# Example usage

schema = """
Table: employees
Columns:
employee_id INTEGER
employee_name VARCHAR
salary NUMERIC
department_id INTEGER

Table: departments
Columns:
department_id INTEGER
department_name VARCHAR
"""

question = "Show the top 5 employees with highest salaries"

sql = generate_sql(question, schema)

print("Generated SQL:")
print(sql)
