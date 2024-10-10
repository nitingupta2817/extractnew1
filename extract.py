import streamlit as st
import requests
from bs4 import BeautifulSoup
import re
import pandas as pd
from io import BytesIO

# Regular expression for matching email addresses
email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

def extract_emails_from_website(url):
    try:
        # Send HTTP request to the website
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Check for request errors

        # Parse the HTML content
        soup = BeautifulSoup(response.content, 'html.parser')

        # Find all 'mailto' links
        mailtos = [a['href'][7:] for a in soup.find_all('a', href=True) if a['href'].startswith('mailto:')]

        # Find all text in the HTML
        text = soup.get_text()

        # Find all emails in the text using regular expressions
        emails_in_text = re.findall(email_pattern, text)

        # Combine emails from mailto links and text
        emails = list(set(mailtos + emails_in_text))

        return emails
    except Exception as e:
        st.error(f"Error fetching {url}: {e}")
        return []

def extract_emails_from_websites(websites):
    all_emails = []
    for website in websites:
        st.write(f"Extracting emails from {website}...")
        emails = extract_emails_from_website(website)
        for email in emails:
            all_emails.append({"Website": website, "Email": email})
    return all_emails

def create_excel_file(email_data):
    # Create a DataFrame from the list of dictionaries
    df = pd.DataFrame(email_data)
    # Save the DataFrame to a BytesIO object
    output = BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)  # Move to the beginning of the BytesIO buffer
    return output

# Streamlit UI
st.title("Website Email Extractor and Exporter")

# Text area for inputting a list of website URLs
st.write("Paste a list of website URLs (one per line):")
websites_input = st.text_area("Enter websites", value="https://www.espine.in\nhttps://www.venusremedies.com")

# Button to trigger email extraction
if st.button("Extract Emails"):
    if websites_input:
        # Split the input into individual URLs by newline and strip spaces
        websites = [url.strip() for url in websites_input.splitlines() if url.strip()]

        if websites:
            email_data = extract_emails_from_websites(websites)

            if email_data:
                # Create the Excel file
                excel_file = create_excel_file(email_data)

                # Provide a download button for the Excel file
                st.download_button(
                    label="Download Emails as Excel",
                    data=excel_file,
                    file_name="emails_extracted.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
                st.success("Emails extracted successfully!")
            else:
                st.warning("No emails found.")
        else:
            st.warning("Please enter valid URLs.")
    else:
        st.warning("Please enter at least one website URL.")
