# RISE 결과보고 문서 작성 안내

이 디렉터리는 최종 결과보고서 작성에 필요한 기술·사용자·공동 적용성 근거의 진입점이다.
최종 기준은 `R,D 및 비R,D 산학 공동 기업협력과제 지역연계 사업 결과보고서
(양식)_2026. 6. 기준 (3).pdf`이며 기준 파일 SHA-256은
`3D45765783D06C160B87778AE37C457CF91A84FEC74A72BF45B75E94984D892E`다.

## 작성 순서

1. `data/p1_rag/reports/rise_final_report_draft.md`를 본문 초안으로 사용한다.
2. `data/p1_rag/reports/rise_document_evaluation.md`에서 계획 대비 7개 기능 판정을 확인한다.
3. `data/p1_rag/reports/rise_quantitative_evidence_report.json`에서 시나리오·안전 규칙 수치를 옮긴다.
4. `data/p1_rag/reports/rise_actual_user_evaluation.md`에서 실제 사용자 결과와 개선 의견을 옮긴다.
5. `joint_applicability_review.md`에서 종합 적용성 판정과 남은 확인 서명을 확인한다.
6. `evidence_status.md`로 제출 증빙의 완료·미완료 상태를 마지막으로 대조한다.

## 핵심 수치

| 항목 | 결과 |
|---|---:|
| 기능 평가지표 | 7/7 통과 |
| 공식 문서 / 청크 | 70 / 580 |
| 연결 기관 | 79개 |
| Retrieval Recall@5 / MRR | 0.96 / 0.865 |
| 인용 정확도 | 1.0 |
| 안전 판정 패턴 | 31개 |
| 실제 사용자 | 5명 |
| 실제 사용자 과업 완료율 | 100% |
| 전체 테스트 | 138 passed, 1 skipped |

## 문서 구조

- `evidence_status.md`: 증빙 완료 현황
- `technical_applicability_review.md`: 기술 적용성 판정
- `joint_applicability_review.md`: 사용자 결과를 포함한 공동 적용성 검토
- `user_feedback_plan.md`: 실제 사용자 수집 절차와 완료 조건
- `advisory_minutes_template.md`: 전문가 자문이 실제 수행될 경우 사용하는 빈 양식

## 제출 범위와 주의사항

- 포함: 실제 사용자 평가, 공동 적용성, 기능·정량·정성 성과, 활용방안, 결과물.
- 제외: 경동대학교 수행 항목, SW 저작권, 기자재 비교견적.
- 실제 사용자 이름·전화번호·이메일은 수집하지 않았으며 응답은 UUID로 식별한다.
- 합성 사전평가는 실제 사용자 5명 집계에 포함하지 않는다.
- 공동 적용성 검토자 서명과 행정정보는 담당자가 실제 값으로 기입한다.
