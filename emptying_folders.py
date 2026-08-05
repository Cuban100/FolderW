import os
import tkinter as tk
from tkinter import ttk
import threading
from dotenv import load_dotenv
load_dotenv()
base_dir = '/home/caveman'
def remove_empty_dirs(base_dir):
    """
    Recursively remove all empty directories under the given base_dir.
    
    Args:
    base_dir (str): Path to the root base_dir to start scanning
    
    Returns:
    int: Number of empty directories removed
    """
    # First, count total directories to process
    total_dirs = 0
    for root, dirs, files in os.walk(base_dir, topdown=False):
        total_dirs += len(dirs) + 1  # +1 for the current directory

    def create_progress_window():
        # Tkinter GUI Setup
        window = tk.Tk()
        window.title("Cleanup Empty Directories")
        window.configure(bg="#1e1e1e")
        window.resizable(False, False)

        # Window dimensions and positioning
        screen_width = window.winfo_screenwidth()
        screen_height = window.winfo_screenheight()
        window_width = 400
        window_height = 100
        x_position = screen_width - window_width - 20
        y_position = screen_height - window_height - 50
        window.geometry(f"{window_width}x{window_height}+{x_position}+{y_position}")

        # Configure the progress bar style
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Neon.Horizontal.TProgressbar",
            troughcolor="#3e3e3e",
            background="#39ff14",
        )
        
        # Label for current action
        status_label = tk.Label(window, text="Scanning directories...", 
                                fg="#39FF14", bg="#1e1e1e", font=("Arial", 10))
        status_label.pack(pady=10)

        # Progress bar
        progress = ttk.Progressbar(window, length=300, mode='determinate', style="Neon.Horizontal.TProgressbar")
        progress.pack(pady=10)

        # Percentage label
        percent_label = tk.Label(window, text="0%", 
                                fg="#39FF14", bg="#1e1e1e", font=("Arial", 10))
        percent_label.pack(pady=5)

        # Function to destroy the window after 3600ms (3 seconds)
        def destroy_window():
            window.destroy()

        # Schedule the window to be destroyed after 3600ms (3 seconds)
        window.after(5600, destroy_window)

        return window, progress, status_label, percent_label


    # Create progress window
    window, progress_bar, status_label, percent_label = create_progress_window()

    empty_dirs_count = 0
    
    def process_directories():
        nonlocal empty_dirs_count
        try:
            # Walk through the base_dir tree from bottom to top
            for root, dirs, files in os.walk(base_dir, topdown=False):
                # Update progress
                current_progress = int((empty_dirs_count / total_dirs) * 100)
                progress_bar['value'] = current_progress
                percent_label.config(text=f"{current_progress}%")
                status_label.config(text=f"Processed {empty_dirs_count} directories")

                # Check if this base_dir is empty (no files and no non-empty subdirectories)
                if not files and not dirs:
                    try:
                        os.rmdir(root)
                        empty_dirs_count += 1
                    except Exception as e:
                        status_label.config(text=f"Error: {e}")
                
                # Check for directories that became empty after removing their contents
                for dir_name in dirs:
                    full_path = os.path.join(root, dir_name)
                    try:
                        # If the subbase_dir is now empty, remove it
                        if not os.listdir(full_path):
                            os.rmdir(full_path)
                            empty_dirs_count += 1
                    except Exception as e:
                        status_label.config(text=f"Error: {e}")
            
            # Final update
            progress_bar['value'] = 100
            percent_label.config(text="100%")
            status_label.config(text=f"Completed. Removed {empty_dirs_count} directories")
            
            # Optional: close window after 2 seconds
            # window.after(2000, window.destroy)
        
        except Exception as e:
            status_label.config(text=f"Unexpected error: {e}")
        
        return empty_dirs_count
    
    # Window dimensions and positioning
    

    # Run in a separate thread to keep UI responsive
    thread = threading.Thread(target=process_directories)
    thread.start()

    # Start the Tkinter event loop
    window.mainloop()

    # Wait for the thread to complete
    thread.join()
    
    return empty_dirs_count
    
# You can add this to your existing script
def cleanup_empty_dirs():
    """Cleanup empty directories in the backup base directory."""
    print(f"Starting cleanup of empty directories in: {base_dir}")
    try:
        removed_count = remove_empty_dirs(base_dir)
        print(f"Cleanup complete. Removed {removed_count} empty directories.")
    except Exception as e:
        print(f"An error occurred during directory cleanup: {e}")

# Modify the existing __main__ block or add this
if __name__ == "__main__": 
    cleanup_empty_dirs()
    # Add this to run empty directory cleanup after back