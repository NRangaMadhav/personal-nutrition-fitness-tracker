"""Opt-in live smoke checks. Uses synthetic data and deletes its own account."""

import argparse
import io
import secrets
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import boto3
import httpx
import psycopg
from dotenv import dotenv_values
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]


def checked(response, expected=(200, 201, 204)):
    if response.status_code not in expected:
        detail = response.json().get("detail", "Request failed")
        raise RuntimeError(f"{response.request.method} {response.request.url.path}: HTTP {response.status_code}: {detail}")
    return response.json() if response.content else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai", action="store_true", help="Make live provider requests with synthetic inputs; may incur charges.")
    args = parser.parse_args()
    config = dotenv_values(ROOT / ".env")
    email = f"local-smoke-{uuid.uuid4().hex}@example.com"
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    user_id = None
    try:
        with httpx.Client(base_url="http://127.0.0.1:8000", timeout=180, trust_env=False) as client:
            token = checked(client.post("/auth/register", json={"email": email, "password": secrets.token_urlsafe(24)}))
            client.headers["Authorization"] = f"Bearer {token['access_token']}"
            user_id = checked(client.get("/auth/me"))["id"]
            checked(client.put("/profile", json={
                "date_of_birth": "1995-01-01", "sex": "male", "height_cm": 170,
                "current_weight_kg": 70, "goal_weight_kg": 68, "timezone": "UTC",
            }))
            suggestion = checked(client.get("/targets/recommend"))
            checked(client.put("/targets", json={
                "effective_date": today,
                **{key: suggestion[key] for key in ("calories_target", "protein_target", "water_target_ml")},
            }))
            checked(client.post("/water", json={"amount_ml": 250, "logged_at": now}))
            meal = checked(client.post("/meals", json={
                "meal_type": "lunch", "logged_at": now,
                "items": [{"food_name": "Synthetic test meal", "quantity": 1, "unit": "portion",
                           "calories": 300, "protein": 20, "carbs": 35, "fat": 10, "fiber": 5}],
            }))
            supplement = checked(client.post("/supplements", json={"name": "Test supplement", "dosage": 1, "unit": "dose"}))
            checked(client.post("/supplement-logs", json={"supplement_id": supplement["id"], "log_date": today, "taken": True}))
            dashboard = checked(client.get("/dashboard", params={"date": today}))
            assert dashboard["totals"]["calories"] == 300
            assert dashboard["totals"]["water_ml"] == 250
            assert dashboard["bmi"] == 24.22
            for period in ("day", "week", "month"):
                checked(client.get(f"/history/{period}", params={"date": today}))
            print("PASS: account, profile, targets, manual logs, dashboard calculations, day/week/month history.")

            image = Image.new("RGB", (800, 400), "white")
            drawing = ImageDraw.Draw(image)
            drawing.text((40, 100), "Lunch: rice 200 g and beans 100 g", fill="black", font_size=35)
            data = io.BytesIO()
            image.save(data, format="PNG")
            image_id = checked(client.post("/images", files={"file": ("synthetic-meal.png", data.getvalue(), "image/png")}))["id"]
            original = client.get(f"/images/{image_id}")
            assert original.status_code == 200 and original.content == data.getvalue()
            checked(client.put(f"/meals/{meal['id']}", json={
                "meal_type": meal["meal_type"], "logged_at": meal["logged_at"], "image_id": image_id,
                "items": [{key: value for key, value in item.items() if key != "id"} for item in meal["items"]],
            }))
            assert checked(client.get("/dashboard", params={"date": today}))["meals"][0]["image_id"] == image_id
            print("PASS: private MinIO upload, exact original-byte retrieval, meal image attachment.")

            if args.ai:
                for provider in ("nvidia", "groq"):
                    draft = checked(client.post("/ai-log", json={
                        "text": "I drank 500 ml water.", "occurred_at": now, "provider": provider,
                    }))
                    assert draft["status"] == "draft"
                    events = draft["events"]
                    assert len(events) == 1 and events[0]["type"] == "water" and events[0]["amount_ml"] == 500
                    assert checked(client.get("/dashboard", params={"date": today}))["totals"]["water_ml"] == 250
                    checked(client.post(f"/ai-log/{draft['id']}/discard"))
                    print(f"PASS: live {provider} text extraction produced a draft without confirmed writes.")
                mixed = checked(client.post("/ai-log", json={
                    "text": "For lunch I ate 200 g cooked rice. I drank 250 ml water, measured my body weight at 70 kg, and took fish oil.",
                    "occurred_at": now, "provider": "auto",
                }))
                assert {event["type"] for event in mixed["events"]} == {"meal", "water", "weight", "supplement"}
                checked(client.post(f"/ai-log/{mixed['id']}/discard"))
                print("PASS: live mixed input extracted meal, water, weight, and supplement events.")
                draft = checked(client.post("/ai-log", json={
                    "text": "I drank 500 ml water.", "occurred_at": now, "provider": "auto",
                }))
                checked(client.post("/ai-log/confirm", json={"draft_id": draft["id"], "events": draft["events"]}))
                repeated = client.post("/ai-log/confirm", json={"draft_id": draft["id"], "events": draft["events"]})
                assert repeated.status_code in (200, 409)
                assert checked(client.get("/dashboard", params={"date": today}))["totals"]["water_ml"] == 750
                print("PASS: explicit confirmation, persisted totals, repeated-confirmation protection.")
                for text in ("", "Read this synthetic meal label as lunch. Its quantities are written on the image."):
                    draft = checked(client.post("/ai-log", json={
                        "text": text, "image_id": image_id, "occurred_at": now, "provider": "auto",
                    }))
                    assert draft["status"] == "draft" and draft["events"]
                    reviewed = [event for event in draft["events"] if event["type"] == "meal"]
                    assert reviewed
                    for event in reviewed:
                        event["meal_type"] = "lunch"
                        for item in event["items"]:
                            for key, value in {"quantity": 1, "unit": "portion", "calories": 100,
                                               "protein": 5, "carbs": 15, "fat": 2, "fiber": 1}.items():
                                if item[key] is None:
                                    item[key] = value
                    confirmation = checked(client.post("/ai-log/confirm", json={"draft_id": draft["id"], "events": reviewed}))
                    for record in confirmation["records"]:
                        confirmed_meal = checked(client.get(f"/meals/{record['id']}"))
                        assert confirmed_meal["image_id"] == image_id
                    print(f"PASS: live {'image-plus-text' if text else 'image-only'} extraction, review correction and image-linked confirmation via {draft['provider']}.")
                coach = checked(client.post("/coach", json={
                    "question": "How much water did I log and what was my protein intake?",
                    "start_date": today, "end_date": today, "provider": "auto",
                }))
                assert coach["answer"] and coach["facts"]
                totals = checked(client.get("/dashboard", params={"date": today}))["totals"]
                assert coach["facts"]["summary"]["totals"]["water_ml"] == totals["water_ml"] == 750
                assert coach["facts"]["summary"]["totals"]["protein"] == totals["protein"]
                print(f"PASS: live grounded Coach via {coach['provider']}.")
    finally:
        with psycopg.connect(config["DATABASE_URL"]) as connection:
            keys = []
            if user_id is not None:
                keys = connection.execute("SELECT object_key FROM stored_images WHERE user_id = %s", (user_id,)).fetchall()
            connection.execute("DELETE FROM users WHERE email = %s", (email,))
        if keys:
            storage = boto3.client(
                "s3", endpoint_url=config["OBJECT_STORAGE_ENDPOINT"],
                aws_access_key_id=config["OBJECT_STORAGE_ACCESS_KEY"],
                aws_secret_access_key=config["OBJECT_STORAGE_SECRET_KEY"], region_name="us-east-1",
            )
            for (key,) in keys:
                storage.delete_object(Bucket=config["OBJECT_STORAGE_BUCKET"], Key=key)
        print("Cleaned up this smoke check's synthetic account, records, and image objects.")


if __name__ == "__main__":
    main()
