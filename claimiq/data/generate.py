"""Synthetic claim packs. No real PHI — say this out loud in the demo.

Three packs, each engineered to exercise a different part of the pipeline:

  CLM-2024-0917  clean         -> straight-through processing, the happy path
  CLM-2024-1043  incomplete    -> missing discharge summary + missing fields
  CLM-2024-1188  inconsistent  -> duplicate invoice, date outside policy period,
                                  coding/diagnosis mismatch, policy sublimit
                                  breach. This is the one you demo live.
"""
from __future__ import annotations

from pathlib import Path

DATA = Path(__file__).parent / "claims"


# --------------------------------------------------------------------------
# CLAIM A — clean
# --------------------------------------------------------------------------
CLEAN = {
    "01_claim_form.txt": """
CLAIM NOTIFICATION FORM
Claim Reference: CLM-2024-0917
Date of Notification: 14 March 2024

Policyholder: Margaret A. Whitfield
Policy Number: POL-88213-A
Date of Birth: 07 June 1979
Contact: m.whitfield@example.com | +44 7700 900412

Incident Date: 09 March 2024
Incident Type: Road traffic accident - rear impact
Location: A34 northbound, Junction 9, Oxfordshire

Description of Incident:
Insured vehicle was stationary in traffic when struck from behind by a third
party vehicle. Insured reported immediate neck pain and attended A&E the same
day. No loss of consciousness reported.

Injuries Claimed: Cervical strain (whiplash), lower back pain
Third Party Involved: Yes - Registration KX21 LTR
Police Reference: TVP/2024/03/44921

Adjuster Assigned: J. Okonkwo
Signature: M. A. Whitfield        Date: 14 March 2024
""",
    "02_medical_report.txt": """
OXFORD UNIVERSITY HOSPITALS NHS FOUNDATION TRUST
Emergency Department - Clinical Report

Patient Name: Margaret A. Whitfield
Date of Birth: 07/06/1979
NHS Number: 485 777 3456
Attendance Date: 09 March 2024, 16:42
Discharge Date: 09 March 2024, 20:15

Presenting Complaint:
Patient presented following a rear-impact road traffic collision occurring at
approximately 15:30 on 09 March 2024. Complains of posterior neck pain radiating
to both shoulders, and lumbar discomfort.

Examination:
Alert and orientated, GCS 15/15. Cervical spine: tenderness over C4-C6
paraspinal musculature. Range of motion reduced by approximately 30 percent in
rotation bilaterally. No midline bony tenderness. Neurovascular examination of
upper limbs intact. Lumbar spine: mild paraspinal tenderness L3-L5. Straight leg
raise negative bilaterally.

Investigations:
Canadian C-Spine Rule applied - imaging not indicated. No radiography performed.

Diagnosis:
1. Acute cervical strain (whiplash-associated disorder grade II) - ICD-10 S13.4
2. Lumbar strain - ICD-10 S33.5

Treatment Plan:
- Simple analgesia: paracetamol 1g QDS, ibuprofen 400mg TDS with food
- Early mobilisation advised, no collar
- Referral to outpatient physiotherapy
- GP follow-up in 2 weeks

Prior Relevant Conditions: None documented. No previous spinal injury.

Clinician: Dr. S. Ramanathan, MBBS MRCEM
GMC: 7214498
Date: 09 March 2024
""",
    "03_physio_invoice.txt": """
OXFORD PHYSIOTHERAPY ASSOCIATES
17 Banbury Road, Oxford, OX2 6NN
Provider Registration: HCPC PH-44821

INVOICE

Invoice Number: OPA-2024-3387
Invoice Date: 22 April 2024
Patient: Margaret A. Whitfield
Claim Reference: CLM-2024-0917
Service Period: 25 March 2024 to 19 April 2024

--------------------------------------------------------------
Description                          Qty    Unit      Amount
--------------------------------------------------------------
Initial assessment (45 min)            1    85.00      85.00
Physiotherapy session (30 min)         6    55.00     330.00
Manual therapy - cervical              4    45.00     180.00
Home exercise programme                1    35.00      35.00
--------------------------------------------------------------
                                       Subtotal:      630.00
                                       VAT (0%):        0.00
                                       TOTAL GBP:     630.00
--------------------------------------------------------------

Payment Terms: 30 days
Bank: Barclays | Sort 20-65-21 | Acc 40028871
""",
    "04_discharge_summary.txt": """
OXFORD UNIVERSITY HOSPITALS NHS FOUNDATION TRUST
Emergency Department Discharge Summary

Patient: Margaret A. Whitfield
DOB: 07/06/1979
Attendance: 09 March 2024
Discharged: 09 March 2024 at 20:15

Summary of Care:
Attended following RTC. Assessed by ED clinician. Cervical and lumbar strain
diagnosed clinically. Imaging not indicated per Canadian C-Spine Rule.
Analgesia provided. Discharged home in stable condition, ambulant.

Discharge Medications:
- Paracetamol 500mg tablets, 32 tablets, 1-2 QDS PRN
- Ibuprofen 400mg tablets, 24 tablets, TDS with food

Follow-up Arranged:
- Outpatient physiotherapy referral submitted
- GP review in 14 days

Fit for discharge: Yes
Discharging Clinician: Dr. S. Ramanathan
""",
    "05_policy_schedule.txt": """
NORTHBRIDGE INSURANCE PLC
Personal Injury Policy Schedule

Policy Number: POL-88213-A
Policyholder: Margaret A. Whitfield
Address: 42 Hollybush Row, Oxford, OX1 1HU

Period of Insurance:
Effective From: 01 January 2024
Expires: 31 December 2024

Cover Summary:
Personal Injury - Overall Limit:            GBP 50,000
Medical Expenses - Annual Limit:            GBP 10,000
  Sub-limit: Physiotherapy                  GBP  2,500
  Sub-limit: Diagnostic Imaging             GBP  1,500
Loss of Earnings - Weekly Benefit:          GBP    400
Excess (per claim):                         GBP    250

General Exclusions:
- Injury arising from participation in motorsport
- Pre-existing conditions declared or undeclared at inception
- Treatment not clinically recommended by a registered practitioner
- Cosmetic procedures

Premium Paid: GBP 428.00 (annual, paid in full 28 December 2023)
""",
}


# --------------------------------------------------------------------------
# CLAIM B — incomplete
# --------------------------------------------------------------------------
INCOMPLETE = {
    "01_claim_form.txt": """
CLAIM NOTIFICATION FORM
Claim Reference: CLM-2024-1043
Date of Notification: 02 May 2024

Policyholder: Devon R. Achterberg
Policy Number: POL-91556-C
Date of Birth: 23 November 1988
Contact: (phone not provided)

Incident Date: 27 April 2024
Incident Type: Slip and fall - commercial premises
Location: Riverside Retail Park, Reading

Description of Incident:
Claimant slipped on an unmarked wet surface near the entrance of a retail unit
and fell, landing on the right side. Reported pain in the right shoulder and
wrist. Attended minor injuries unit the following morning.

Injuries Claimed: Right shoulder injury, right wrist pain
Third Party Involved: Yes - premises operator
Witness Details: (not provided)

Adjuster Assigned: (unassigned)
Signature: D. Achterberg      Date: 02 May 2024
""",
    "02_medical_report.txt": """
ROYAL BERKSHIRE HOSPITAL
Minor Injuries Unit - Clinical Note

Patient Name: Devon R. Achterberg
Date of Birth: 23/11/1988
Attendance Date: 28 April 2024

Presenting Complaint:
Fall on wet floor previous day. Pain right shoulder and right wrist.

Examination:
Right shoulder: tenderness over the acromioclavicular joint, abduction limited
to approximately 90 degrees by pain. No obvious deformity.
Right wrist: swelling over the distal radius, tender on palpation, painful
range of motion.

Investigations:
X-ray right wrist and right shoulder requested.

RADIOLOGY RESULT: [pending at time of writing - report to follow]

Provisional Diagnosis:
1. Right wrist injury - fracture to be excluded
2. Right shoulder soft tissue injury / possible AC joint sprain

Treatment:
Wrist immobilised in futura splint. Analgesia advised. Fracture clinic referral.

Clinician: Nurse Practitioner L. Osei
Date: 28 April 2024

NOTE: Formal radiology report and fracture clinic outcome not attached.
""",
    "03_invoice.txt": """
READING ORTHOPAEDIC CLINIC
Invoice

Invoice Number: ROC-5512
Invoice Date: 30 May 2024
Patient: D. Achterberg

--------------------------------------------------------------
Description                                        Amount
--------------------------------------------------------------
Orthopaedic consultation                           240.00
Wrist splint - custom                              145.00
Follow-up review                                   120.00
--------------------------------------------------------------
                                     TOTAL:        505.00
--------------------------------------------------------------
""",
    "04_policy_schedule.txt": """
NORTHBRIDGE INSURANCE PLC
Personal Injury Policy Schedule

Policy Number: POL-91556-C
Policyholder: Devon R. Achterberg

Period of Insurance:
Effective From: 15 February 2024
Expires: 14 February 2025

Cover Summary:
Personal Injury - Overall Limit:            GBP 25,000
Medical Expenses - Annual Limit:            GBP  5,000
  Sub-limit: Physiotherapy                  GBP  1,500
Excess (per claim):                         GBP    150

General Exclusions:
- Pre-existing conditions
- Injury while under the influence of alcohol or drugs
- Treatment outside the United Kingdom
""",
}


# --------------------------------------------------------------------------
# CLAIM C — inconsistent (the live demo)
# --------------------------------------------------------------------------
INCONSISTENT = {
    "01_claim_form.txt": """
CLAIM NOTIFICATION FORM
Claim Reference: CLM-2024-1188
Date of Notification: 11 June 2024

Policyholder: Terrence J. Vasquez-Hollis
Policy Number: POL-77401-B
Date of Birth: 14 February 1971
Contact: tvh1971@example.com | +44 7700 900338

Incident Date: 03 June 2024
Incident Type: Road traffic accident - side impact
Location: Kingsway, Manchester M1

Description of Incident:
Claimant states his vehicle was struck on the driver's side at a junction.
Reports severe lower back pain and left knee injury. States he was taken by
ambulance to hospital and admitted overnight.

Injuries Claimed: Lumbar spine injury, left knee ligament damage
Third Party Involved: Yes
Police Reference: GMP/2024/06/11204

Signature: T. Vasquez-Hollis      Date: 11 June 2024
""",
    "02_medical_report.txt": """
MANCHESTER ROYAL INFIRMARY
Emergency Department Clinical Report

Patient Name: Terrence J. Vasquez-Hollis
Date of Birth: 14/02/1971
Attendance Date: 03 June 2024, 11:20
Discharge Date: 03 June 2024, 14:05

Presenting Complaint:
Patient self-presented to the Emergency Department by private vehicle following
a minor road traffic collision earlier that morning. Complains of lower back
discomfort. No knee complaint recorded at presentation.

Examination:
Ambulant on arrival, no distress. Lumbar spine: mild paraspinal tenderness L4-L5.
Full range of motion. Straight leg raise negative bilaterally. Neurovascular
examination intact. Left knee examined - no effusion, no joint line tenderness,
full range of motion, ligamentous examination stable (anterior drawer negative,
Lachman negative, collateral ligaments intact).

Investigations:
No imaging clinically indicated.

Diagnosis:
1. Mild lumbar soft tissue strain - ICD-10 S33.5

Treatment Plan:
- Simple analgesia
- Advised to remain active
- No follow-up required unless symptoms persist beyond 4 weeks

Disposal: Discharged home same day, ambulant. NOT ADMITTED.

Clinician: Dr. A. Nakamura, MBBS
GMC: 7788201
Date: 03 June 2024
""",
    "03_invoice_physio.txt": """
NORTHERN SPINE & SPORTS THERAPY
Unit 4, Deansgate Business Centre, Manchester

INVOICE

Invoice Number: NSS-2024-0881
Invoice Date: 18 July 2024
Patient: T. Vasquez-Hollis
Claim Reference: CLM-2024-1188
Service Period: 10 June 2024 to 16 July 2024

--------------------------------------------------------------
Description                          Qty    Unit      Amount
--------------------------------------------------------------
Initial assessment                     1    120.00     120.00
Physiotherapy - intensive              18    95.00    1710.00
Hydrotherapy session                   12   110.00    1320.00
Spinal manipulation                     8   130.00    1040.00
Rehabilitation equipment hire           1   450.00     450.00
--------------------------------------------------------------
                                       Subtotal:     4640.00
                                       VAT (20%):     928.00
                                       TOTAL GBP:    5568.00
--------------------------------------------------------------
""",
    "04_invoice_physio_dup.txt": """
NORTHERN SPINE & SPORTS THERAPY
Unit 4, Deansgate Business Centre, Manchester

INVOICE

Invoice Number: NSS-2024-0881
Invoice Date: 21 July 2024
Patient: T. Vasquez-Hollis
Claim Reference: CLM-2024-1188
Service Period: 10 June 2024 to 16 July 2024

--------------------------------------------------------------
Description                          Qty    Unit      Amount
--------------------------------------------------------------
Initial assessment                     1    120.00     120.00
Physiotherapy - intensive              18    95.00    1710.00
Hydrotherapy session                   12   110.00    1320.00
Spinal manipulation                     8   130.00    1040.00
Rehabilitation equipment hire           1   450.00     450.00
Additional administrative fee           1    95.00      95.00
--------------------------------------------------------------
                                       Subtotal:     4735.00
                                       VAT (20%):     947.00
                                       TOTAL GBP:    5682.00
--------------------------------------------------------------
""",
    "05_invoice_surgical.txt": """
PENNINE PRIVATE ORTHOPAEDICS
Invoice

Invoice Number: PPO-19947
Invoice Date: 09 August 2024
Patient: Terrence J. Vasquez-Hollis
Claim Reference: CLM-2024-1188

--------------------------------------------------------------
Description                                Code      Amount
--------------------------------------------------------------
Arthroscopic ACL reconstruction, left knee  29888    6800.00
Theatre and anaesthetic charges                      2200.00
Overnight inpatient stay (1 night)                    850.00
Post-operative review                                 180.00
--------------------------------------------------------------
                                     Subtotal:      10030.00
                                     TOTAL GBP:     10030.00
--------------------------------------------------------------

Procedure Date: 05 August 2024
Surgeon: Mr. D. Fairbairn FRCS
""",
    "06_policy_schedule.txt": """
NORTHBRIDGE INSURANCE PLC
Personal Injury Policy Schedule

Policy Number: POL-77401-B
Policyholder: Terrence J. Vasquez-Hollis

Period of Insurance:
Effective From: 01 July 2023
Expires: 30 June 2024

Cover Summary:
Personal Injury - Overall Limit:            GBP 40,000
Medical Expenses - Annual Limit:            GBP 12,000
  Sub-limit: Physiotherapy                  GBP  2,000
  Sub-limit: Surgical Procedures            GBP  8,000
Excess (per claim):                         GBP    500

General Exclusions:
- Pre-existing conditions
- Treatment not clinically recommended by a registered practitioner
- Elective or cosmetic procedures

RENEWAL STATUS: Policy lapsed 30 June 2024. Renewal not completed.
""",
    "07_police_report.txt": """
GREATER MANCHESTER POLICE
Road Traffic Collision Report

Reference: GMP/2024/06/11204
Date of Collision: 03 June 2024
Time: Approximately 09:15
Location: Kingsway junction with Barlow Road, Manchester M19

Vehicles Involved:
Vehicle 1: Silver saloon, registration ND19 KPX - driver T. Vasquez-Hollis
Vehicle 2: White light goods vehicle, registration MJ22 TRW

Collision Circumstances:
Vehicle 2 was reversing from a loading bay at low speed and made contact with
the offside rear quarter panel of Vehicle 1, which was stationary. Damage
limited to minor scuffing and a dented rear quarter panel on Vehicle 1. No
damage recorded to the driver's door or driver's side front.

Injuries Reported at Scene: None reported by either driver.
Ambulance Attended: No.
Both drivers declined medical attention at the scene.

Speed Estimate: Vehicle 2 travelling at under 5 mph at point of impact.

Reporting Officer: PC 4471 Hargreaves
Date of Report: 03 June 2024
""",
}


PACKS = {
    "CLM-2024-0917": CLEAN,
    "CLM-2024-1043": INCOMPLETE,
    "CLM-2024-1188": INCONSISTENT,
}


def write_packs(root: Path | None = None) -> list[Path]:
    root = Path(root) if root else DATA
    written = []
    for claim_id, files in PACKS.items():
        d = root / claim_id
        d.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            p = d / name
            p.write_text(body.strip() + "\n", encoding="utf-8")
            written.append(p)
    return written


if __name__ == "__main__":
    paths = write_packs()
    print(f"Wrote {len(paths)} files across {len(PACKS)} claim packs -> {DATA}")
    for cid, files in PACKS.items():
        print(f"  {cid}: {len(files)} documents")
