# Frontend flow

```mermaid
flowchart TD
    OPEN["Open the app"] --> ASK["Type a database question"]
    ASK --> SUBMIT["Click Start query workflow"]
    SUBMIT --> TRACK["Watch the live tracker and generated SQL"]
    TRACK --> MODE{"Approval required?"}
    MODE -- "No" --> RUN["Query runs read-only"]
    RUN --> SHOW["See results in the app"]
    MODE -- "Yes" --> WAIT["See that approval is pending"]
    WAIT --> REVIEW["Approver reviews SQL in email or approval page"]
    REVIEW --> DECISION{"Approve?"}
    DECISION -- Yes --> RUN
    DECISION -- No --> REJECT["See rejected; query is not run"]
    WAIT --> EXPIRE["If not decided in time: see expired; query is not run"]
    TRACK --> ERROR["If something fails: see the error in the tracker"]
```

## In simple words

1. Open the frontend and type your question.
2. Click **Start query workflow**. The tracker shows the steps and SQL as they
   happen. You can also expand **Explore database schema** to see the tables.
3. If approval is **off**, the query runs read-only and the results appear.
4. If approval is **on**, the app says it is waiting. The approver reviews the
   email or approval page:
   - **Approve:** the query runs read-only and results appear.
   - **Reject:** the query is not run; the tracker shows the decision and comment.
   - **No decision before expiry:** the query is not run.
5. If generation, validation, email, or execution fails, the tracker displays
   the error.

The browser receives tracker updates over a single live connection. It does not
send a new status request every few seconds.
