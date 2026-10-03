import os
import tempfile
from pathlib import Path
from typing import Optional
from app.utils.logging_config import logger


class FileUtils:
    """File handling utilities."""
    
    @staticmethod
    def save_temp_file(content: bytes, filename: str) -> str:
        """Save content to a temporary file and return the path."""
        try:
            # Create temp directory if it doesn't exist
            temp_dir = Path("temp")
            temp_dir.mkdir(exist_ok=True)
            
            # Create a safe filename
            safe_filename = FileUtils.sanitize_filename(filename)
            temp_path = temp_dir / safe_filename
            
            # Write the content
            with open(temp_path, 'wb') as f:
                f.write(content)
            
            logger.info(f"Saved temporary file: {temp_path}")
            return str(temp_path)
            
        except Exception as e:
            logger.error(f"Failed to save temporary file: {str(e)}")
            raise
    
    @staticmethod
    def delete_file(file_path: str) -> bool:
        """Delete a file safely."""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Deleted file: {file_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to delete file {file_path}: {str(e)}")
            return False
    
    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Sanitize filename to be safe for filesystem."""
        # Remove or replace problematic characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, '_')
        
        # Limit length
        if len(filename) > 255:
            name, ext = os.path.splitext(filename)
            filename = name[:255 - len(ext)] + ext
        
        return filename
    
    @staticmethod
    def get_file_extension(filename: str) -> str:
        """Get file extension from filename."""
        return os.path.splitext(filename)[1].lower()
    
    @staticmethod
    def cleanup_temp_directory():
        """Clean up all files in the temp directory."""
        try:
            temp_dir = Path("temp")
            if temp_dir.exists():
                for file in temp_dir.iterdir():
                    if file.is_file():
                        FileUtils.delete_file(str(file))
                logger.info("Cleaned up temp directory")
        except Exception as e:
            logger.error(f"Failed to cleanup temp directory: {str(e)}")
