import sys
import os

class SysPathManager:
    @staticmethod
    def import_from(base_path, folder_name, module_name):
        """
        Validates the path, updates sys.path, and imports the module.
        """
        # 1. Construct the full path (e.g., /bin/power)
        full_path = f"{base_path}/{folder_name}".replace('//', '/')
        
        # 2. Check if the directory actually exists on the filesystem
        try:
            os.stat(full_path)
        except OSError:
            raise OSError(f"Directory not found: {full_path}")
            
        # 3. Add to sys.path if it is not already there
        if full_path not in sys.path:
            sys.path.append(full_path)
            
        # 4. Import and return the module dynamically
        try:
            return __import__(module_name)
        except ImportError as e:
            raise ImportError(f"Could not find '{module_name}' inside '{full_path}': {e}")
