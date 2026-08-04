"""Fast bulk seed: inserts students for all enrolled schools that don't have any yet."""
import asyncio
import random
from faker import Faker
import asyncpg

DB = "postgresql://postgres:ma0zYn9RzAZbhBOE2Bs235KSwPdTeF4D@127.0.0.1:5432/lazeims"
EXAM_ID = "a18722b2-22e6-40e1-b004-c9998db8e07f"

COMPULSORY = ["011", "012", "013", "021", "022", "033", "041"]
PHYSICS = "031"
CHEMISTRY = "032"
BOOK_KEEPING = "062"
COMMERCE = "061"
LITERATURE = "024"
COMPUTER = "036"


def pick_electives(rng):
    codes = []
    if rng.random() < 0.30:
        if rng.random() < 0.10:
            codes.append(CHEMISTRY)
        else:
            codes.extend([CHEMISTRY, PHYSICS])
    else:
        if rng.random() < 0.55:
            codes.extend([BOOK_KEEPING, COMMERCE])
    if rng.random() < 0.20:
        codes.append(LITERATURE)
    if rng.random() < 0.15:
        codes.append(COMPUTER)
    return codes


async def main():
    rng = random.Random(99)
    fake = Faker()
    Faker.seed(99)
    conn = await asyncpg.connect(DB)

    # subject map
    rows = await conn.fetch(
        "SELECT s.code, es.id FROM subjects s JOIN exam_subjects es ON es.subject_id = s.id WHERE es.exam_id = $1",
        EXAM_ID,
    )
    subject_map = {r["code"]: r["id"] for r in rows}
    print(f"Subject map: {len(subject_map)} subjects")

    # schools without students
    schools = await conn.fetch(
        """SELECT s.id, s.centre_number FROM schools s
           JOIN exam_schools es ON es.school_id = s.id
           WHERE es.exam_id = $1
           AND s.id NOT IN (SELECT DISTINCT school_id FROM exam_students WHERE exam_id = $1)
           ORDER BY s.centre_number""",
        EXAM_ID,
    )
    print(f"Seeding {len(schools)} schools...")

    total_students = 0
    for i, school in enumerate(schools):
        count = rng.randint(25, 50)
        for seq in range(1, count + 1):
            cid = f"{school['centre_number']}/{seq:04d}"
            is_male = rng.random() < 0.5
            sex = "M" if is_male else "F"
            first = fake.first_name_male() if is_male else fake.first_name_female()
            middle = (fake.first_name_male() if is_male else fake.first_name_female()) if rng.random() < 0.7 else None
            surname = fake.last_name()

            sid = await conn.fetchval(
                """INSERT INTO exam_students (student_id, exam_id, school_id, first_name, middle_name, surname, sex, created_at, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NOW()) RETURNING id""",
                cid, EXAM_ID, school["id"], first, middle, surname, sex,
            )

            codes = COMPULSORY + pick_electives(rng)
            for code in codes:
                await conn.execute(
                    "INSERT INTO exam_student_subjects (exam_student_id, exam_subject_id, created_at, updated_at) VALUES ($1, $2, NOW(), NOW())",
                    sid, subject_map[code],
                )
            total_students += 1

        if (i + 1) % 50 == 0:
            print(f"  {i+1}/{len(schools)} schools, {total_students} students")

    print(f"Done! {total_students} students across {len(schools)} schools")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
