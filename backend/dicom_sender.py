import os
import json
import glob
import pydicom
from pynetdicom import AE
from pynetdicom.sop_class import CTImageStorage, RTStructureSetStorage

def get_destinations():
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dest_path = os.path.join(root_dir, "dest.json")
    if not os.path.exists(dest_path):
        dest_path = "dest.json"
        if not os.path.exists(dest_path):
            print("Warning: dest.json not found.")
            return []
            
    try:
        with open(dest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [dest for dest in data.get("dicom_destinations", []) if dest.get("is_active", False)]
    except Exception as e:
        print(f"Error loading dest.json: {e}")
        return []

def send_dicom_series(series_path: str):
    destinations = get_destinations()
    if not destinations:
        print("No active DICOM destinations configured.")
        return

    dicom_files = glob.glob(os.path.join(series_path, "*.dcm"))
    if not dicom_files:
        print(f"No DICOM files found in {series_path}")
        return

    datasets = []
    for f in dicom_files:
        try:
            ds = pydicom.dcmread(f)
            datasets.append(ds)
        except Exception as e:
            print(f"Error reading DICOM file {f}: {e}")

    if not datasets:
        return

    ae = AE()
    ae.add_requested_context(CTImageStorage)
    ae.add_requested_context(RTStructureSetStorage)

    for dest in destinations:
        ae_title = dest.get("ae_title")
        ip = dest.get("ip_address")
        port = int(dest.get("port", 11112))
        print(f"Routing {len(datasets)} files to {ae_title} at {ip}:{port}...")
        
        try:
            assoc = ae.associate(ip, port, ae_title=ae_title)
            if assoc.is_established:
                success_count = 0
                for ds in datasets:
                    status = assoc.send_c_store(ds)
                    if status is not None:
                        status_code = getattr(status, 'Status', status)
                        if status_code == 0x0000:
                            success_count += 1
                assoc.release()
                print(f"Successfully sent {success_count}/{len(datasets)} files to {ae_title}.")
            else:
                print(f"Failed to associate with {ae_title} at {ip}:{port}.")
        except Exception as e:
            print(f"Error sending DICOM to {ae_title}: {e}")
