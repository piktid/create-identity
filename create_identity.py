#!/usr/bin/env python3
"""
Minimal script to generate a new identity from a brief and promote it into your library.

This script performs the basic Create Identity workflow:
1. Submit a creation job from a brief JSON file (or build one from a reference
   image via preset extraction with --from-image)
2. Wait for the job to complete and fetch draft images
3. Download every draft to output/drafts/
4. Pick a draft (interactive prompt, or --auto-promote to skip)
5. Promote the chosen draft into a full identity
6. Poll preprocessing until the new identity is ready
7. Save provenance metadata to output/<identity_code>/metadata.json

Authentication uses an API token generated at https://app.on-model.com/profile?tab=tokens

The brief JSON file must match the body shape of POST /identity/create. See
briefs/scandinavian-model.json and briefs/expert-prompt.json for examples, or
the docs at https://docs.piktid.com/docs/v2/concepts/identities/create.
"""

import argparse
import http.client
import json
import random
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


class CreateIdentity:
    def __init__(self, base_url, token, brief_path=None, from_image=None,
                 output_folder="output", auto_promote=False, picked_draft=None,
                 explicit_name=None, num_variations=3, save_brief=None):
        self.base_url = base_url.rstrip("/")
        self.brief_path = Path(brief_path) if brief_path else None
        self.from_image = Path(from_image) if from_image else None
        self.output_folder = Path(output_folder)
        self.auto_promote = auto_promote
        self.picked_draft = picked_draft
        self.explicit_name = explicit_name
        self.num_variations = num_variations
        self.save_brief = Path(save_brief) if save_brief else None

        self.access_token = token
        self.brief = None
        self.job_id = None
        self.drafts = []
        self.chosen_draft_id = None
        self.identity_code = None
        self.preprocessing_job_id = None

    def get_auth_headers(self):
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    def _request_with_retry(self, method, url, max_retries=5, initial_delay=1.0,
                            max_delay=60.0, **kwargs):
        """Authenticated request with exponential backoff on 429."""
        delay = initial_delay
        request_func = getattr(requests, method.lower())

        for attempt in range(max_retries + 1):
            headers = {**kwargs.pop("headers", {}), **self.get_auth_headers()}
            response = request_func(url, headers=headers, **kwargs)

            if response.status_code == 401:
                print("Token expired or invalid. Generate a new one at "
                      "https://app.on-model.com/profile?tab=tokens")
                return response

            if response.status_code != 429:
                return response

            if attempt < max_retries:
                jitter = delay * 0.2 * (2 * random.random() - 1)
                wait_time = min(delay + jitter, max_delay)
                print(f"Rate limited (429). Waiting {wait_time:.1f}s before retry "
                      f"{attempt + 1}/{max_retries}...")
                time.sleep(wait_time)
                delay = min(delay * 2, max_delay)
            else:
                print(f"Rate limited (429). Max retries ({max_retries}) exceeded.")

        return response

    def load_brief(self):
        """Resolve self.brief from either --brief or --from-image."""
        if self.from_image is not None:
            self.brief = self.brief_from_image(self.from_image)
            if self.brief is None:
                return False
            if self.save_brief is not None:
                self.save_brief.parent.mkdir(parents=True, exist_ok=True)
                with open(self.save_brief, "w") as f:
                    json.dump(self.brief, f, indent=2)
                print(f"Extracted brief saved to {self.save_brief}")
            return True

        if self.brief_path is None:
            print("Either --brief or --from-image must be provided")
            return False
        if not self.brief_path.exists():
            print(f"Brief file not found: {self.brief_path}")
            return False
        try:
            with open(self.brief_path) as f:
                self.brief = json.load(f)
        except json.JSONDecodeError as e:
            print(f"Invalid JSON in brief: {e}")
            return False

        if "instructions" not in self.brief or not self.brief["instructions"]:
            print("Brief must contain a non-empty 'instructions' list")
            return False

        print(f"Loaded brief from {self.brief_path}")
        return True

    def brief_from_image(self, reference_path):
        """Upload a reference image, extract identity attributes via the
        preset extraction endpoint, and wrap them in a brief ready to submit.

        This mirrors the 'From Image' button in the Identities wizard on
        app.on-model.com: read attributes from the reference, fill the
        structured fields, and let the user proceed with normal submission.
        The reference image itself is NOT passed to the generator — a new
        person is generated from the extracted attributes.
        """
        if not reference_path.exists():
            print(f"Reference image not found: {reference_path}")
            return None

        file_id = self._upload_image(reference_path)
        if not file_id:
            return None

        print("Extracting identity attributes from the reference...")
        response = self._request_with_retry(
            "post",
            f"{self.base_url}/preset/extract-from-image",
            json={"file_id": file_id, "type": "identity_creation"},
        )
        if response.status_code != 200:
            print(f"Preset extraction failed: {response.status_code} {response.text}")
            return None

        extracted = response.json().get("instruction_data") or {}
        # Keep only the canonical user-facing groups (appearance/face/hair).
        # The lambda hard-defaults camera/outfit/style/scene anyway, so any
        # other groups in the extraction would be ignored downstream.
        instruction = {
            k: v for k, v in extracted.items()
            if k in ("appearance", "face", "hair") and isinstance(v, dict) and v
        }
        if not instruction:
            print("Preset extraction returned no usable structured fields")
            return None

        instruction["num_variations"] = self.num_variations
        instruction["options"] = {"ar": "3:4", "size": "2K", "format": "jpg"}

        print(f"Built brief from extracted attributes: "
              f"{', '.join(sorted(instruction.keys() - {'num_variations', 'options'}))}")
        return {
            "instructions": [instruction],
            "options": {"model": "auto"},
            "name": self.explicit_name or f"From {reference_path.stem}",
        }

    def _upload_image(self, image_path):
        """Upload an image via POST /upload and return its file_id."""
        print(f"Uploading {image_path.name}...")

        response = self._request_with_retry(
            "post",
            f"{self.base_url}/upload",
            json={"filename": image_path.name},
        )
        if response.status_code != 200:
            print(f"Failed to get upload URL: {response.status_code} {response.text}")
            return None
        info = response.json()
        upload_url = info["upload_url"]
        if self.base_url.startswith("https://") and upload_url.startswith("http://"):
            upload_url = upload_url.replace("http://", "https://", 1)

        with open(image_path, "rb") as f:
            image_data = f.read()
        parsed = urlparse(upload_url)
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=120)
        else:
            conn = http.client.HTTPConnection(parsed.netloc, timeout=120)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        conn.request(
            "PUT",
            path,
            body=image_data,
            headers={"Content-Type": info["content_type"]},
        )
        put_response = conn.getresponse()
        put_response.read()
        conn.close()

        if put_response.status not in [200, 201]:
            print(f"Failed to upload to S3: HTTP {put_response.status}")
            return None
        print(f"Uploaded as file_id={info['file_id']}")
        return info["file_id"]

    def submit_creation(self):
        """POST /identity/create with the brief, returns the job_id."""
        print("Submitting identity creation job...")
        response = self._request_with_retry(
            "post",
            f"{self.base_url}/identity/create",
            json=self.brief,
        )
        if response.status_code != 202:
            print(f"Failed to submit creation job: {response.status_code} {response.text}")
            return False

        data = response.json()
        self.job_id = data["job_id"]
        total = data.get("total_variations", "?")
        print(f"Creation job dispatched: {self.job_id} ({total} draft(s) requested)")
        return True

    def wait_for_drafts(self, max_wait_time=900, check_interval=5):
        """Poll /jobs/<id>/status until the creation job completes."""
        print("Waiting for drafts to render...")

        start_time = time.time()
        retry_404 = 0
        max_404 = 10
        time.sleep(3)

        while True:
            if time.time() - start_time > max_wait_time:
                print(f"Timeout: job took longer than {max_wait_time} seconds")
                return False

            response = self._request_with_retry(
                "get",
                f"{self.base_url}/jobs/{self.job_id}/status",
            )

            if response.status_code == 404:
                retry_404 += 1
                if retry_404 <= max_404:
                    print(f"Job not visible yet ({retry_404}/{max_404}), waiting...")
                    time.sleep(3)
                    continue
                print(f"Job {self.job_id} not found after {max_404} retries")
                return False

            if response.status_code != 200:
                print(f"Failed to get status: {response.status_code} {response.text}")
                return False

            retry_404 = 0
            status_data = response.json()
            status = status_data.get("status")
            progress = status_data.get("progress", 0)
            print(f"Progress: {progress:.1f}% - Status: {status}")

            if status == "completed":
                return True
            if status in ["failed", "aborted"]:
                print(f"Job ended with status: {status}")
                return False

            time.sleep(check_interval)

    def fetch_drafts(self):
        """GET /jobs/<id>/results, populates self.drafts."""
        print("Fetching draft list...")
        response = self._request_with_retry(
            "get",
            f"{self.base_url}/jobs/{self.job_id}/results",
        )
        if response.status_code != 200:
            print(f"Failed to fetch results: {response.status_code} {response.text}")
            return False

        data = response.json()
        self.drafts = []
        for result in data.get("results", []):
            if result.get("status") != "completed":
                continue
            output_url = None
            if isinstance(result.get("output"), dict):
                output_url = result["output"].get("full_size")
            if not output_url:
                output_url = result.get("url")
            if not output_url:
                continue
            self.drafts.append({
                "image_result_id": result.get("image_result_id") or result.get("id"),
                "image_index": result.get("image_index"),
                "url": output_url,
                "version": result.get("version", 0),
            })

        if not self.drafts:
            print("No completed drafts in job results")
            return False
        print(f"Got {len(self.drafts)} draft(s)")
        return True

    def download_drafts(self):
        """Download every draft into output/drafts/ for review."""
        drafts_dir = self.output_folder / "drafts"
        drafts_dir.mkdir(parents=True, exist_ok=True)

        for draft in self.drafts:
            filename = f"draft_{draft['image_result_id']}.jpg"
            output_path = drafts_dir / filename
            try:
                img_response = requests.get(draft["url"], timeout=60)
                img_response.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(img_response.content)
                draft["local_path"] = str(output_path)
                print(f"Downloaded: {filename}")
            except Exception as e:
                print(f"Failed to download draft {draft['image_result_id']}: {e}")

    def choose_draft(self):
        """Return the image_result_id of the draft to promote."""
        if self.picked_draft is not None:
            for draft in self.drafts:
                if draft["image_result_id"] == self.picked_draft:
                    print(f"Using --pick draft: {self.picked_draft}")
                    return self.picked_draft
            print(f"--pick {self.picked_draft} is not in this job's drafts")
            return None

        if self.auto_promote:
            chosen = self.drafts[0]["image_result_id"]
            print(f"--auto-promote: promoting first draft (id={chosen})")
            return chosen

        print()
        print("Available drafts:")
        for idx, draft in enumerate(self.drafts):
            print(f"  [{idx}] image_result_id={draft['image_result_id']}  "
                  f"local={draft.get('local_path', '?')}")
        print()
        while True:
            try:
                raw = input(f"Pick a draft to promote (0-{len(self.drafts) - 1}, or 'q' to quit): ").strip()
            except EOFError:
                return None
            if raw.lower() in ("q", "quit"):
                return None
            try:
                idx = int(raw)
            except ValueError:
                print("Enter a number or 'q'")
                continue
            if 0 <= idx < len(self.drafts):
                return self.drafts[idx]["image_result_id"]
            print("Out of range")

    def promote(self, image_result_id):
        """POST /identity/promote-generated with the chosen draft id."""
        print(f"Promoting draft {image_result_id} into a permanent identity...")

        payload = {"image_result_id": image_result_id}
        if self.explicit_name:
            payload["name"] = self.explicit_name

        response = self._request_with_retry(
            "post",
            f"{self.base_url}/identity/promote-generated",
            json=payload,
        )

        if response.status_code == 409:
            data = response.json()
            existing = data.get("identity_code")
            if existing:
                print(f"Draft already promoted as identity {existing}")
                self.identity_code = existing
                return True
            print(f"Promotion conflict: {response.text}")
            return False

        if response.status_code != 201:
            print(f"Failed to promote draft: {response.status_code} {response.text}")
            return False

        data = response.json()
        self.identity_code = data["identity_code"]
        self.preprocessing_job_id = data.get("preprocessing_job_id")
        print(f"Promoted to identity {self.identity_code}; preprocessing "
              f"{self.preprocessing_job_id}")
        return True

    def wait_for_preprocessing(self, max_wait_time=600, check_interval=5):
        """Poll /identity/<code>/status until preprocessing completes."""
        if not self.identity_code:
            return False
        print("Waiting for identity preprocessing...")

        start_time = time.time()
        while True:
            if time.time() - start_time > max_wait_time:
                print(f"Timeout: preprocessing took longer than {max_wait_time} seconds")
                return False

            response = self._request_with_retry(
                "get",
                f"{self.base_url}/identity/{self.identity_code}/status",
            )
            if response.status_code != 200:
                print(f"Failed to get identity status: {response.status_code} {response.text}")
                return False

            data = response.json()
            status = data.get("preprocessing_status")
            print(f"Preprocessing status: {status}")

            if status == "completed":
                return True
            if status == "failed":
                print(f"Preprocessing failed: {data.get('error_message')}")
                return False

            time.sleep(check_interval)

    def save_metadata(self):
        """Write provenance metadata to output/<identity_code>/metadata.json."""
        identity_dir = self.output_folder / (self.identity_code or "unknown")
        identity_dir.mkdir(parents=True, exist_ok=True)

        chosen = next(
            (d for d in self.drafts if d["image_result_id"] == self.chosen_draft_id),
            None,
        )
        if chosen and chosen.get("local_path"):
            source = Path(chosen["local_path"])
            if source.exists():
                target = identity_dir / source.name
                if not target.exists():
                    shutil.copy2(source, target)

        metadata = {
            "identity_code": self.identity_code,
            "preprocessing_job_id": self.preprocessing_job_id,
            "creation_job_id": self.job_id,
            "promoted_image_result_id": self.chosen_draft_id,
            "brief": self.brief,
            "drafts": self.drafts,
        }
        with open(identity_dir / "metadata.json", "w") as f:
            json.dump(metadata, f, indent=2)
        print(f"Provenance saved to {identity_dir / 'metadata.json'}")

    def run(self):
        print("=" * 70)
        print("Create Identity")
        print("=" * 70)

        if not self.load_brief():
            return False
        if not self.submit_creation():
            return False
        if not self.wait_for_drafts():
            return False
        if not self.fetch_drafts():
            return False
        self.download_drafts()

        chosen = self.choose_draft()
        if chosen is None:
            print("No draft picked; exiting without promoting.")
            return False
        self.chosen_draft_id = chosen

        if not self.promote(chosen):
            return False
        if not self.wait_for_preprocessing():
            return False

        self.save_metadata()

        print("=" * 70)
        print(f"Identity ready: {self.identity_code}")
        print("=" * 70)
        return True


def main():
    parser = argparse.ArgumentParser(
        description="Generate a new On-Model identity from a brief and promote it into your library"
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "--brief",
        type=str,
        default=None,
        help="Path to a brief JSON file (see briefs/ for examples)",
    )
    input_group.add_argument(
        "--from-image",
        type=str,
        default=None,
        help="Path to a reference image. Identity attributes (appearance, face, "
             "hair) are extracted from the image via preset extraction and used "
             "to build the brief. The reference image is NOT passed to the "
             "generator — a new person is generated from the extracted attributes.",
    )

    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="API token from https://app.on-model.com/profile?tab=tokens",
    )
    parser.add_argument(
        "--output-folder",
        type=str,
        default="output",
        help="Output folder for drafts and provenance (default: output)",
    )
    parser.add_argument(
        "--variations",
        type=int,
        default=3,
        help="Number of draft variations when using --from-image (default: 3, max: 8). "
             "Ignored when --brief is used (variations come from the brief).",
    )
    parser.add_argument(
        "--save-brief",
        type=str,
        default=None,
        help="When using --from-image, also save the generated brief JSON to this path.",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="https://v2.api.piktid.com",
        help="API base URL (default: https://v2.api.piktid.com)",
    )
    parser.add_argument(
        "--auto-promote",
        action="store_true",
        help="Skip the interactive picker and promote the first completed draft.",
    )
    parser.add_argument(
        "--pick",
        type=int,
        default=None,
        help="Promote a specific draft by image_result_id (skips the interactive picker).",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Optional explicit name for the promoted identity. If omitted, the "
             "promotion falls back to the brief's job-level 'name'.",
    )

    args = parser.parse_args()

    workflow = CreateIdentity(
        base_url=args.base_url,
        token=args.token,
        brief_path=args.brief,
        from_image=args.from_image,
        output_folder=args.output_folder,
        auto_promote=args.auto_promote,
        picked_draft=args.pick,
        explicit_name=args.name,
        num_variations=args.variations,
        save_brief=args.save_brief,
    )

    success = workflow.run()
    if not success:
        exit(1)


if __name__ == "__main__":
    main()
