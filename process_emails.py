"""
Simple script to process emails manually.
Run this script whenever you want to check for new emails and process them.
"""
import requests
import sys
from app.utils.logging_config import logger


def process_emails():
    """Trigger email processing via API."""
    try:
        logger.info("Starting manual email processing...")
        
        # Call the API endpoint
        response = requests.post("http://localhost:8000/api/process?limit=10", timeout=120)
        
        if response.status_code == 200:
            results = response.json()
            logger.info(f"Processing completed: {results}")
            print(f"\n✅ Email Processing Results:")
            print(f"   Emails processed: {results.get('emails_processed', 0)}")
            print(f"   Attachments processed: {results.get('attachments_processed', 0)}")
            print(f"   Invoices extracted: {results.get('invoices_extracted', 0)}")
            print(f"   Invoices stored: {results.get('invoices_stored', 0)}")
            
            if results.get('errors'):
                print(f"\n⚠️  Errors encountered: {len(results['errors'])}")
                for error in results['errors']:
                    print(f"   - {error.get('error', 'Unknown error')}")
            
            return True
        else:
            logger.error(f"API call failed with status {response.status_code}")
            print(f"❌ Failed to process emails. Status code: {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError:
        logger.error("Cannot connect to server. Make sure the server is running.")
        print("❌ Cannot connect to server. Please start the server first with: python run.py")
        return False
    except Exception as e:
        logger.error(f"Error processing emails: {str(e)}")
        print(f"❌ Error: {str(e)}")
        return False


if __name__ == "__main__":
    success = process_emails()
    sys.exit(0 if success else 1)