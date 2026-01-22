🦺 SAP Daily Safety Monitoring

PostgreSQL • Python • SQL • WhatsApp Automation

📌 Overview

This project is a daily automated monitoring system for Safety Accountability Program (SAP) achievement.

The reporting channel is intentionally designed via WhatsApp, considering that the primary users are field supervisors who may not be comfortable accessing BI dashboards (such as Tableau) or opening external links. WhatsApp ensures high adoption, daily visibility, and immediate awareness of SAP performance.

🎯 Business Problem

Before this system was implemented:
	•	SAP monitoring was performed manually
	•	Reporting required supervisors to open spreadsheets or dashboards
	•	Daily SAP visibility was inconsistent
	•	Monitoring results were often delayed
	•	SAP tended to become a reporting formality, not a daily accountability tool

As a result, SAP performance was not effectively used to drive daily safety behavior and accountability.

💡 Solution

This system automates the entire SAP monitoring workflow:
	1.	Retrieves daily SAP plan and actual data from PostgreSQL
	2.	Calculates achievement scores per supervisor
	3.	Applies key business logic:
	•	Plan vs actual comparison
	•	Score capping at 100%
	•	Fair normalization across active indicators
	4.	Generates simple and clear PNG visual reports
	5.	Automatically delivers reports via WhatsApp, eliminating the need for dashboards or external links

This design ensures SAP performance is visible, accessible, and actionable every day.

🧠 Key Logic Highlights (SQL)
	•	Conditional aggregation is used to count actual activities per indicator
	•	Division by zero protection is applied when plan values are zero
	•	Achievement scores are capped at 100% to prevent over-scoring
	•	Scores are normalized based on the number of active indicators to ensure fair comparison

🔄 Workflow

PostgreSQL
↓
SQL aggregation and scoring
↓
Python processing
↓
PNG report generation
↓
WhatsApp auto blast to supervisors

🛠️ Tech Stack
	•	Database: PostgreSQL
	•	Language: Python
	•	Query: Advanced SQL (CTE, CASE WHEN, aggregation)
	•	Output: PNG visual reports
	•	Automation: Batch script and WhatsApp-based distribution

📊 Output
	•	Daily SAP achievement per supervisor
	•	Achievement per indicator (Hazard, Inspection, Observation, Coaching, etc.)
	•	Visual PNG reports optimized for mobile viewing

Sample outputs can be added in the sample_output folder with sensitive data anonymized.

📈 Impact
	•	Reporting time reduced from hours to minutes
	•	Manual reporting errors eliminated
	•	Consistent and repeatable SAP monitoring
	•	Fair performance comparison across supervisors
	•	High adoption due to WhatsApp-based delivery

👤 Author

Sam
Data Analyst | SQL • Python • Data Automation

Focused on business-driven analytics, practical automation, and real-world mining safety operational reporting.
