import streamlit as st
import pandas as pd
from serpapi import GoogleSearch
from io import BytesIO
import plotly.express as px
from crewai import Agent, Task, Crew
from langchain_openai import ChatOpenAI
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import re
import uuid

# --- Load secrets from Streamlit ---
EMAIL_ADDRESS = st.secrets["EMAIL_ADDRESS"]
EMAIL_PASSWORD = st.secrets["EMAIL_PASSWORD"]
SERP_API_KEY = st.secrets["SERP_API_KEY"]
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]

# --- Page config ---
st.set_page_config(page_title="Google Jobs + AI Summary", layout="wide")
st.title("🔍 Google Jobs Dashboard + AI Summary Email Sender")

# --- Initialize session state ---
for key, default in {
    "trigger_search": False,
    "df": None,
    "job_title": "",
    "location": "",
    "recipient_email": "",
    "summary_generated": False,
    "summary_text": "",
    "excel_data": None,
    "date_filter": "All",
    "company_summary": ""
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# --- Sidebar inputs ---
with st.sidebar:
    st.header("Job Search Settings")
    job_title = st.text_input("Job Title", value=st.session_state["job_title"] or "Data Scientist")
    location = st.text_input("Location", value=st.session_state["location"] or "New York")
    recipient_email = st.text_input("📧 Email to send summary", value=st.session_state["recipient_email"])
    date_filter = st.selectbox("🗓️ Filter by Date", ["All", "Last 24 hrs", "Last 7 days", "Last 30 days", "Older"])
    if st.button("Search Jobs"):
        st.session_state.update({
            "trigger_search": True,
            "job_title": job_title,
            "location": location,
            "recipient_email": recipient_email,
            "summary_generated": False,
            "summary_text": "",
            "date_filter": date_filter,
            "company_summary": ""
        })

# --- Fetch jobs from SerpAPI ---
def fetch_google_jobs(job_title, location):
    params = {
        "engine": "google_jobs",
        "q": f"{job_title} in {location}",
        "api_key": SERP_API_KEY
    }
    try:
        search = GoogleSearch(params)
        results = search.get_dict()
        jobs = results.get("jobs_results", [])
        listings = []
        for job in jobs:
            highlights = job.get("job_highlights", [])
            job_type = highlights[0].get("items", ["N/A"])[0] if len(highlights) >= 1 else "N/A"
            experience = highlights[1].get("items", ["N/A"])[0] if len(highlights) >= 2 else "N/A"
            listings.append({
                "Title": job.get("title"),
                "Company": job.get("company_name"),
                "Location": job.get("location"),
                "Description": job.get("description", '')[:300] + "...",
                "Apply Link": job.get("related_links", [{}])[0].get("link", "#"),
                "Via": job.get("via", "Direct"),
                "Posted": job.get("detected_extensions", {}).get("posted_at", "Unknown"),
                "Job Type": job_type,
                "Experience Level": experience
            })
        return listings
    except Exception as e:
        st.error(f"❌ Error fetching jobs: {str(e)}")
        return []

# --- Add date category ---
def categorize_posted_date(posted):
    if 'hour' in str(posted).lower():
        return 'Last 24 hrs'
    elif 'day' in str(posted).lower():
        try:
            num = int(posted.split()[0])
            if num <= 7:
                return 'Last 7 days'
            elif num <= 30:
                return 'Last 30 days'
            else:
                return 'Older'
        except:
            return 'Unknown'
    return 'Older'

# --- Convert to Excel ---
def convert_to_excel(data):
    df = pd.DataFrame(data)
    df['Apply Link'] = df['Apply Link'].apply(
        lambda url: f'=HYPERLINK("{url}", "Click Here")' if url and url != "#" else "N/A")
    output = BytesIO()
    with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False, sheet_name='Jobs')
        workbook = writer.book
        worksheet = writer.sheets['Jobs']
        hyperlink_format = workbook.add_format({'font_color': 'blue', 'underline': 1})
        link_col_idx = df.columns.get_loc("Apply Link")
        for row in range(len(df)):
            formula = df.iloc[row]["Apply Link"]
            if formula.startswith('=HYPERLINK'):
                worksheet.write_formula(row + 1, link_col_idx, formula, hyperlink_format)
            else:
                worksheet.write(row + 1, link_col_idx, formula)
    output.seek(0)
    return output

# --- AI Summary ---
def generate_summary(df):
    try:
        llm = ChatOpenAI(model_name="gpt-3.5-turbo", temperature=0.6, api_key=OPENAI_API_KEY)
        analyst_agent = Agent(
            role="Job Market Analyst",
            goal="Summarize job trends from a DataFrame in a structured bullet-point format",
            backstory="You specialize in analyzing job data and presenting concise, structured insights.",
            verbose=False,
            llm=llm,
        )
        csv_sample = df[['Title', 'Company', 'Location', 'Job Type', 'Experience Level', 'Via', 'Posted']].to_csv(index=False)
        # Calculate the most common posting source
        most_common_source = df['Via'].value_counts().idxmax()
        most_common_count = df['Via'].value_counts().max()
        total_jobs = len(df)
        task = Task(
            description=(f"Analyze the following job data and provide a structured summary in bullet-point format:\n{csv_sample}\n"
                         f"Include the following sections with specific details, associating each point with the relevant company, location, and job title from the data:\n"
                         f"- **Posting Sources**:\n"
                         f"  - Indeed:\n"
                         f"    - Data Scientist at Lafayette 148, Inc in Brooklyn, NY.\n"
                         f"    - Data Scientist, Amazon Connect at Amazon Development Center U.S., Inc. in New York, NY.\n"
                         f"  - Careers At Paramount:\n"
                         f"    - Data Scientist at Paramount in New York, NY.\n"
                         f"  - LinkedIn:\n"
                         f"    - Data Science Manager, New Product & Partnerships (Rider) at Lyft in New York, NY.\n"
                         f"  - Capital One Careers:\n"
                         f"    - Senior Associate, Data Science at Capital One in New York, NY.\n"
                         f"  - Ladders:\n"
                         f"    - Lead Data Scientist, Research, Multimodal Search at Google in New York, NY.\n"
                         f"  - Peloton Careers:\n"
                         f"    - Data Scientist, Product Analytics at Peloton Interactive, Inc in New York, NY.\n"
                         f"  - EFinancialCareers:\n"
                         f"    - Manager, Data Scientist - Card Customer Management at Capital One in New York, NY.\n"
                         f"  - NYC Jobs - NYC.gov:\n"
                         f"    - Senior Research Data Scientist at New York City in Queens, NY.\n"
                         f"  - Out of these, more sources are from {most_common_source} with {most_common_count} out of {total_jobs} job postings.\n"
                         f"- **Job Types**:\n"
                         f"  - Data Scientist:\n"
                         f"    - Lafayette 148, Inc in Brooklyn, NY.\n"
                         f"    - Paramount in New York, NY.\n"
                         f"    - Amazon Development Center U.S., Inc in New York, NY.\n"
                         f"    - Peloton Interactive, Inc in New York, NY.\n"
                         f"  - Senior Associate, Data Science:\n"
                         f"    - Capital One in New York, NY.\n"
                         f"  - Senior Research Data Scientist:\n"
                         f"    - New York City in Queens, NY.\n"
                         f"- **Experience Levels**:\n"
                         f"  - Data Scientist roles require 2+ years of experience and skills in data querying languages, scripting languages, and machine learning/statistical modeling, e.g., Data Scientist at Lafayette 148, Inc in Brooklyn, NY, and Amazon Development Center U.S., Inc in New York, NY.\n"
                         f"  - Some positions require a Master's or Ph.D. degree in a quantitative discipline, e.g., Senior Research Data Scientist at New York City in Queens, NY.\n"
                         f"- **Date Categories**:\n"
                         f"  - Most job postings are recent, within the last 7 days, indicating active recruitment efforts, e.g., Data Scientist at Lafayette 148, Inc in Brooklyn, NY, and Paramount in New York, NY.\n"
                         f"  - Only 1 job posting is older, with the posting source being Capital One Careers, e.g., Senior Associate, Data Science at Capital One in New York, NY.\n"
                         f"- **Key Locations**:\n"
                         f"  - New York, NY: Lafayette 148, Inc, Amazon Development Center U.S., Inc, Paramount, Lyft, Capital One, Peloton Interactive, Inc.\n"
                         f"  - Brooklyn, NY: Lafayette 148, Inc.\n"
                         f"  - Queens, NY: New York City.\n"
                         f"- **Top Companies**:\n"
                         f"  - Lafayette 148, Inc (e.g., Data Scientist in Brooklyn, NY).\n"
                         f"  - Paramount (e.g., Data Scientist in New York, NY).\n"
                         f"  - Amazon Development Center U.S., Inc (e.g., Data Scientist, Amazon Connect in New York, NY).\n"
                         f"  - Capital One (e.g., Senior Associate, Data Science in New York, NY).\n"
                         f"- **Overall Trend**:\n"
                         f"  - Strong demand for Data Scientists in New York, NY, with recent job postings indicating active recruitment efforts. Companies like Lafayette 148, Inc, Paramount, and Amazon Development Center U.S., Inc are seeking experienced professionals in this field."),
            agent=analyst_agent,
            expected_output="A detailed bullet-point summary with sections for Posting Sources, Job Types, Experience Levels, Date Categories, Key Locations, Top Companies, and Overall Trend, with specific company, location, and job title details from the data."
        )
        crew = Crew(agents=[analyst_agent], tasks=[task], process="sequential")
        result = crew.kickoff()
        output = result.tasks_output[-1].raw.strip()
        if "great answer" in output.lower() or len(output.split()) < 10:
            raise ValueError("Uninformative summary output received.")
        return output
    except Exception as e:
        return f"⚠️ AI summary could not be generated properly. Error: {e}"

# --- Send email ---
def send_email(content, recipient_email, excel_data, company_summary):
    if not re.match(r"[^@]+@[^@]+\.[^@]+", recipient_email):
        return False, "Invalid recipient email address."
    msg = MIMEMultipart()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = recipient_email
    msg["Subject"] = "📊 AI Job Summary + Excel Attachment"
    email_content = f"AI-Generated Job Market Summary:\n\n{content}\n\n🏢 Company-Wise Job Openings:\n{company_summary}"
    msg.attach(MIMEText(email_content, "plain", "utf-8"))
    if excel_data:
        attachment = MIMEApplication(excel_data.read(), _subtype='vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        attachment.add_header('Content-Disposition', 'attachment', filename="JobListings.xlsx")
        msg.attach(attachment)
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True, None
    except Exception as e:
        return False, f"Email sending error: {str(e)}"

# --- Main flow ---
if st.session_state.get("trigger_search"):
    st.session_state["trigger_search"] = False
    with st.spinner("🔄 Fetching jobs and generating insights..."):
        jobs = fetch_google_jobs(st.session_state["job_title"], st.session_state["location"])
        if not jobs:
            st.error("❌ No jobs found or API limit reached.")
        else:
            df = pd.DataFrame(jobs)
            df['Date Posted Category'] = df['Posted'].apply(categorize_posted_date)
            # Apply date filter
            selected_filter = st.session_state["date_filter"]
            if selected_filter != "All":
                df = df[df['Date Posted Category'] == selected_filter]
            st.session_state["df"] = df
            st.subheader("📋 Job Listings")
            for i, row in df.iterrows():
                with st.expander(f"{i+1}. {row['Title']} - {row['Company']}"):
                    st.markdown(f"**Location:** {row['Location']}")
                    st.markdown(f"**Type:** {row['Job Type']} | **Experience:** {row['Experience Level']}")
                    st.markdown(f"**Via:** {row['Via']} | **Posted:** {row['Posted']}")
                    st.markdown(f"**Description:** {row['Description']}")
                    st.markdown(f"[Apply Here]({row['Apply Link']})")
            
            # Company-wise summary
            st.subheader("🏢 Company-Wise Job Openings")
            company_counts = df['Company'].value_counts().reset_index()
            company_counts.columns = ['Company', 'Number of Openings']
            company_summary = ", ".join(
                f"{row['Company']} - {row['Number of Openings']} {'job' if row['Number of Openings'] == 1 else 'openings'}"
                for _, row in company_counts.iterrows()
            )
            st.session_state["company_summary"] = company_summary
            st.markdown(company_summary)
            
            excel_data = convert_to_excel(df)
            st.session_state["excel_data"] = excel_data
            st.download_button("📅 Download Excel", data=excel_data, file_name="Job_Listings.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            # Charts
            st.subheader("📊 Dashboard")
            charts = {
                "Posting Sources": df['Via'].value_counts().reset_index(name="Count").rename(columns={'index': 'Via'}),
                "Job Types": df['Job Type'].value_counts().reset_index(name="Count").rename(columns={'index': 'Job Type'}),
                "Top Locations": df['Location'].value_counts().nlargest(10).reset_index(name="Count").rename(columns={'index': 'Location'}),
                "Experience Levels": df['Experience Level'].value_counts().reset_index(name="Count").rename(columns={'index': 'Experience Level'}),
                "Posting Recency": df['Date Posted Category'].value_counts().reset_index(name="Count").rename(columns={'index': 'Date Posted Category'}),
            }
            for title, data in charts.items():
                fig = px.bar(data, x=data.columns[0], y="Count", title=title)
                fig.update_traces(marker_color='#1f77b4')
                st.plotly_chart(fig, use_container_width=True)

# --- Summary + Email ---
st.subheader("🧐 AI Summary + 📤 Email")
if st.session_state["summary_generated"]:
    st.write("### AI-Generated Summary")
    st.write(st.session_state["summary_text"])

if st.button("Generate Summary & Send Email"):
    if not st.session_state["recipient_email"]:
        st.error("❌ Please provide a valid recipient email address.")
    elif st.session_state["df"] is None or st.session_state["df"].empty:
        st.error("❌ Please perform a job search first.")
    elif not st.session_state["company_summary"]:
        st.error("❌ Company-wise summary is missing. Please perform a job search first.")
    else:
        with st.spinner("🧠 Generating summary..."):
            summary = generate_summary(st.session_state["df"])
            st.session_state["summary_text"] = summary
            st.session_state["summary_generated"] = True
        with st.spinner("📤 Sending email..."):
            sent, error = send_email(summary, st.session_state["recipient_email"], st.session_state["excel_data"], st.session_state["company_summary"])
            if sent:
                st.success("✅ Summary and Excel file sent successfully!")
            else:
                st.error(f"❌ Failed to send email: {error}")
