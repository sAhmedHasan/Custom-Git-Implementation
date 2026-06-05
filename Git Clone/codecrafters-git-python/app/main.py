import sys
import os
from zlib import decompress, compress
import hashlib
from pathlib import Path
import urllib.request
import struct
import zlib


        
def write_tree(directory):
    # Collect all entries in this directory
    entries = []
    
    for path in sorted(Path(directory).iterdir()):
        if path.name == ".git":
            continue
        
        if path.is_file():
            # Hash the file as a blob
            with open(path, "rb") as f:
                file_content = f.read()
            blob_content = b"blob " + str(len(file_content)).encode("utf-8") + b"\0" + file_content
            blob_hash = hashlib.sha1(blob_content).hexdigest()
            
            # Store blob object
            object_dir = ".git/objects/" + blob_hash[:2]
            os.makedirs(object_dir, exist_ok=True)
            with open(object_dir + "/" + blob_hash[2:], "wb") as f:
                f.write(compress(blob_content))
            
            # Record entry: mode, name, and binary hash
            mode = b"100644"
            name = path.name.encode("utf-8")
            sha_binary = bytes.fromhex(blob_hash)
            entries.append((mode, name, sha_binary))
        
        elif path.is_dir():
            # Recursively hash subdirectory
            subtree_hash = write_tree(str(path))
            
            # Record entry: mode, name, and binary hash
            mode = b"40000"
            name = path.name.encode("utf-8")
            sha_binary = bytes.fromhex(subtree_hash) # Convert hex string to binary
            entries.append((mode, name, sha_binary))
    
    # Build tree object: header + all entries
    tree_entries = b""
    for mode, name, sha_binary in entries:
        tree_entries += mode + b" " + name + b"\0" + sha_binary
    
    tree_content = b"tree " + str(len(tree_entries)).encode("utf-8") + b"\0" + tree_entries
    tree_hash = hashlib.sha1(tree_content).hexdigest()
    
    # Store tree object
    object_dir = ".git/objects/" + tree_hash[:2]
    os.makedirs(object_dir, exist_ok=True)
    with open(object_dir + "/" + tree_hash[2:], "wb") as f:
        f.write(compress(tree_content))
    
    return tree_hash


def make_pkt_line(data_str):
    total_length = len(data_str) + 4
    hex_prefix = f"{total_length:04x}" #:04x formats the number as a 4-digit hexadecimal string
    return hex_prefix.encode("utf-8") + data_str.encode("utf-8")


def download_packfile(repo_url, commit_sha):
    post_url = f"{repo_url}/git-upload-pack"

    pkt_want = make_pkt_line(f"want {commit_sha}\n") #listing wants
    pkt_flush = b"0000" #done listing wants
    pkt_done = make_pkt_line("done\n") #done talking

    request_body = pkt_want + pkt_flush + pkt_done
    headers = {"Content-Type": "application/x-git-upload-pack-request", "Accept": "application/x-git-upload-pack-result", "User-Agent": "git/2.30.0"}

    request = urllib.request.Request(post_url, data=request_body, headers=headers, method = "POST")

    try:
    # Fire the request over the internet
        with urllib.request.urlopen(request) as response:
            # Read the incoming binary stream from GitHub
            raw_response = response.read()
            print(f"Downloaded packfile data successfully ({len(raw_response)} bytes).")
            return raw_response
        
    except Exception as e:
        print(f"Negotiation phase failed: {e}")
        return None


def get_latest_commit_sha(repo_url):
    try:
        # stich together the discovery URL
        discovery_url = f"{repo_url}/info/refs?service=git-upload-pack"
        headers = {"User-Agent": "git/2.30.0"}
        request = urllib.request.Request(discovery_url, headers=headers)

        # get the response and read the lines
        with urllib.request.urlopen(request) as response:
            lines = response.read().splitlines()

        # iterate through the lines and find the key string, parse out the data, get sha
        for line in lines:
            if b"#" in line or b"0000" in line:
                continue
            if b"refs/heads/main" in line or b"refs/heads/master" in line:
                clean_line = line[4:]
                clean_line = clean_line.split()
                commit_sha = clean_line[0].decode("utf-8")
                return commit_sha
        
        raise RuntimeError("Could not find main or master branch in refs")
    
    except urllib.error.URLError as e:
        raise RuntimeError(f"Failed to fetch from repository: {e}")
    except Exception as e:
        raise RuntimeError(f"Error getting latest commit SHA: {e}")



def unpack_and_save_objects(raw_response, target_dir):
    import struct
    import zlib
    import hashlib
    from pathlib import Path

    pure_packfile = raw_response
    pack_start_idx = pure_packfile.find(b'PACK')
    if pack_start_idx == -1:
        raise ValueError("Invalid packfile stream: 'PACK' magic header missing.")
        
    pure_packfile = pure_packfile[pack_start_idx:]
    header_bytes = pure_packfile[:12]
    signature, version, object_count = struct.unpack("!4sII", header_bytes)
    print(f"Parsing verified Packfile. Version: {version}, Objects: {object_count}")

    cursor = 12
    type_map = {1: b"commit", 2: b"tree", 3: b"blob", 4: b"tag"} 
    
    # Cache objects in memory to easily resolve deltas
    # Format: { sha_hex: (type_bytes, uncompressed_data) }
    objects_cache = {}
    pending_ref_deltas = []


    for _ in range(object_count):
        first_byte = pure_packfile[cursor]
        cursor += 1
        
        obj_type_id = (first_byte & 0b01110000) >> 4
        
        is_multibyte = first_byte & 0b10000000
        while is_multibyte:
            next_byte = pure_packfile[cursor]
            cursor += 1
            is_multibyte = next_byte & 0b10000000
            
        if obj_type_id == 7:  # OBJ_REF_DELTA
            base_sha_bytes = pure_packfile[cursor:cursor+20]
            base_sha_hex = base_sha_bytes.hex()
            cursor += 20
            
            decompressor = zlib.decompressobj()
            delta_data = decompressor.decompress(pure_packfile[cursor:])
            cursor += len(pure_packfile[cursor:]) - len(decompressor.unused_data)
            
            pending_ref_deltas.append((base_sha_hex, delta_data))
            
        elif obj_type_id == 6:  # OBJ_OFS_DELTA
            # Bypass OFS_DELTA offsets
            c = pure_packfile[cursor]
            cursor += 1
            while c & 128:
                c = pure_packfile[cursor]
                cursor += 1
            decompressor = zlib.decompressobj()
            _ = decompressor.decompress(pure_packfile[cursor:])
            cursor += len(pure_packfile[cursor:]) - len(decompressor.unused_data)

        else: # Standard Base Object (1-4)
            obj_type_bytes = type_map.get(obj_type_id, b"unknown")
            decompressor = zlib.decompressobj()
            uncompressed_data = decompressor.decompress(pure_packfile[cursor:])
            cursor += len(pure_packfile[cursor:]) - len(decompressor.unused_data)
            
            header = obj_type_bytes + b" " + str(len(uncompressed_data)).encode("utf-8") + b"\0"
            loose_object_content = header + uncompressed_data
            
            obj_hash = hashlib.sha1(loose_object_content).hexdigest()
            objects_cache[obj_hash] = (obj_type_bytes, uncompressed_data)
            
            obj_dir = Path(target_dir) / ".git" / "objects" / obj_hash[:2]
            obj_dir.mkdir(exist_ok=True, parents=True)
            with open(obj_dir / obj_hash[2:], "wb") as f:
                f.write(zlib.compress(loose_object_content))


    def patch_delta(base_data: bytes, delta_data: bytes) -> bytes:
        """Git's internal algorithm for applying delta instructions."""
        def read_size(data, idx):
            size, shift = 0, 0
            while True:
                byte = data[idx]
                idx += 1
                size |= (byte & 0x7f) << shift
                shift += 7
                if not (byte & 0x80): break
            return size, idx

        # Skip base and target size declarations
        _, idx = read_size(delta_data, 0)
        _, idx = read_size(delta_data, idx)

        target_data = bytearray()
        while idx < len(delta_data):
            cmd = delta_data[idx]
            idx += 1
            if cmd & 0x80: # Copy from base
                offset, size = 0, 0
                if cmd & 0x01: offset |= delta_data[idx]; idx += 1
                if cmd & 0x02: offset |= delta_data[idx] << 8; idx += 1
                if cmd & 0x04: offset |= delta_data[idx] << 16; idx += 1
                if cmd & 0x08: offset |= delta_data[idx] << 24; idx += 1
                if cmd & 0x10: size |= delta_data[idx]; idx += 1
                if cmd & 0x20: size |= delta_data[idx] << 8; idx += 1
                if cmd & 0x40: size |= delta_data[idx] << 16; idx += 1
                if size == 0: size = 0x10000
                target_data.extend(base_data[offset:offset+size])
            elif cmd != 0: # Insert new data
                target_data.extend(delta_data[idx:idx+cmd])
                idx += cmd
        return bytes(target_data)

    # Some deltas depend on other deltas, so we loop until all are resolved
    while pending_ref_deltas:
        unresolved = []
        for base_sha, delta_data in pending_ref_deltas:
            if base_sha in objects_cache:
                base_type, base_content = objects_cache[base_sha]
                resolved_content = patch_delta(base_content, delta_data)
                
                header = base_type + b" " + str(len(resolved_content)).encode("utf-8") + b"\0"
                loose_object_content = header + resolved_content
                
                obj_hash = hashlib.sha1(loose_object_content).hexdigest()
                objects_cache[obj_hash] = (base_type, resolved_content) # Add to cache for chains
                
                obj_dir = Path(target_dir) / ".git" / "objects" / obj_hash[:2]
                obj_dir.mkdir(exist_ok=True, parents=True)
                with open(obj_dir / obj_hash[2:], "wb") as f:
                    f.write(zlib.compress(loose_object_content))
            else:
                unresolved.append((base_sha, delta_data))
        
        # Prevent infinite loops if an OFS_DELTA base is missing
        if len(unresolved) == len(pending_ref_deltas): break 
        pending_ref_deltas = unresolved
            
    print(f"Successfully database-synchronized all {object_count} objects!")

def read_loose_object(target_dir: Path, sha: str) -> tuple[str, bytes]:
    """Helper to read and decompress any loose object from our new .git/objects folder."""
    obj_path = target_dir / ".git" / "objects" / sha[:2] / sha[2:]
    compressed_data = obj_path.read_bytes()
    
    # Decompress and split header from the main body content
    header, content = decompress(compressed_data).split(b"\0", maxsplit=1)
    obj_type, _ = header.split(b" ")
    return obj_type.decode("utf-8"), content

def checkout_repository(target_dir: Path, commit_sha: str):
    """Module 4: Reads the commit tree pointer and materializes files onto disk."""
    # 1. Read the commit object to find its root tree SHA
    _, commit_content = read_loose_object(target_dir, commit_sha)
    
    # Find the line that looks like: "tree 4b825dc...\n"
    for line in commit_content.split(b"\n"):
        if line.startswith(b"tree "):
            root_tree_sha = line[5:].decode("utf-8")
            break
            
    # 2. Start building the folder layout recursively from the root tree
    def instantiate_tree(current_tree_sha, current_path: Path):
        _, tree_content = read_loose_object(target_dir, current_tree_sha)
        
        data = tree_content
        while data:
            # Trees are stored as: "[mode] [name]\0[20-byte binary SHA]"
            space_idx = data.find(b" ")
            mode = data[:space_idx]
            
            null_idx = data.find(b"\0", space_idx)
            name = data[space_idx + 1:null_idx].decode("utf-8")
            
            # Extract the raw 20-byte SHA and convert it to a 40-character hex string
            sha_bytes = data[null_idx + 1:null_idx + 21]
            entry_sha = sha_bytes.hex()
            
            # Prepare the next path item on disk
            item_path = current_path / name
            
            if mode == b"40000": # It's a directory
                item_path.mkdir(exist_ok=True)
                instantiate_tree(entry_sha, item_path) # Recurse deeper
            else: # It's a standard file blob
                _, file_bytes = read_loose_object(target_dir, entry_sha)
                item_path.write_bytes(file_bytes)
                
            # Move our data buffer cursor forward past this entry
            data = data[null_idx + 21:]

    # Begin the recursive folder setup starting at the base directory
    instantiate_tree(root_tree_sha, target_dir)



def main():
  
    command = sys.argv[1]
    if command == "init":
        os.mkdir(".git")
        os.mkdir(".git/objects")
        os.mkdir(".git/refs")
        with open(".git/HEAD", "w") as f:
            f.write("ref: refs/heads/main\n")
        print("Initialized git directory")

    elif command == "cat-file":
        blob_content = sys.argv[3]
        blob_file = ".git/objects/" + blob_content[:2] + "/" + blob_content[2:]
        decompressed_content = decompress(open(blob_file, "rb").read())
        print(decompressed_content.decode("utf-8").split("\0", 1)[1], end="")

    elif command == "hash-object":
        file_name = sys.argv[3]
        with open(file_name, "rb") as f:
            file_content = f.read()
            blob_content = b"blob " + str(len(file_content)).encode("utf-8") + b"\0" + file_content
            hashed_content = hashlib.sha1(blob_content).hexdigest()

        object_dir = ".git/objects/" + hashed_content[:2]
        os.makedirs(object_dir, exist_ok=True)
        with open(object_dir + "/" + hashed_content[2:], "wb") as f:
            f.write(compress(blob_content))
        print(hashed_content, end="")

    elif command == "ls-tree":
        param, tree_sha = sys.argv[2], sys.argv[3]
        if param == "--name-only":
            tree_file = ".git/objects/" + tree_sha[:2] + "/" + tree_sha[2:]
            decompressed_content = decompress(open(tree_file, "rb").read())
            

            _, data = decompressed_content.split(b"\0", 1)
            

            lines = []
            while data:
                null_byte = data.find(b"\0")
                entry_len = null_byte + 1 + 20 
                lines.append(data[:entry_len])
                data = data[entry_len:]

            for line in lines:

                mode, rest = line.split(b" ", 1)
                mode = mode.decode("utf-8")
                
                name = rest.split(b"\0")[0].decode("utf-8")
                print(name)

    elif command == "write-tree":
        tree_hash = write_tree(".")
        print(tree_hash)

    elif command == "commit-tree":
        tree_sha = sys.argv[2]
        parent_sha = sys.argv[4]
        message = sys.argv[6]
        name = str("John Doe")
        email = str("johndoe@email.com")
        timestamp = str("12341234")
        timezone = str("+0000")
        commit_content = f"tree {tree_sha}\nparent {parent_sha}\nauthor {name} <{email}> {timestamp} {timezone}\ncommitter {name} <{email}> {timestamp} {timezone}\n\n{message}\n"
        size = len(commit_content.encode("utf-8")) #get byte size of string
        commit_object = f"commit {size}\0{commit_content}".encode("utf-8")
        commit_hash = hashlib.sha1(commit_object).hexdigest() #get sha1 hash of the object
        object_dir = ".git/objects/" + commit_hash[:2]
        os.makedirs(object_dir, exist_ok=True)
        with open(object_dir + "/" + commit_hash[2:], "wb") as f:
            f.write(compress(commit_object))
        print(commit_hash, end="")

    elif command == "clone":
        repo_url = sys.argv[2]
        repo_dir = sys.argv[3]
        target_dir = Path(repo_dir) 
        git_dir = target_dir / ".git" 
        
        objects_dir = git_dir / "objects"
        target_dir.mkdir(parents=True, exist_ok=True)
        git_dir.mkdir(exist_ok=True)
        objects_dir.mkdir(exist_ok=True)
        (git_dir / "refs").mkdir(exist_ok=True)
        
        with open(git_dir / "HEAD", "w") as f:
            f.write("ref: refs/heads/main\n")

        commit_sha = get_latest_commit_sha(repo_url)
        raw_response = download_packfile(repo_url, commit_sha)
        
        if raw_response:
            unpack_and_save_objects(raw_response, target_dir)
            checkout_repository(target_dir, commit_sha) # Ensure Module 4 functions are called!
        # New Step C: Forward raw bytes to our packfile extractor (Step 2 below)
        unpack_and_save_objects(raw_response, target_dir)

        checkout_repository(target_dir, commit_sha)










    else:
        raise RuntimeError(f"Unknown command #{command}")



if __name__ == "__main__":
    main()
