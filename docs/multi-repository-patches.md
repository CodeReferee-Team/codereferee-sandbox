# 커밋 고정 멀티레포 패치

version 3 서비스 그룹의 추가 레포에도 요청별 패치를 적용한다. 사용자 원격 레포에는 쓰지 않는다.
기준 레포는 기존 `patchDiff`와 `commitSha`를 사용한다. 추가 레포는 설정에 선언된 alias만 허용한다.

[실제 순차 수정·거절·정리 검증과 제외 범위](multi-repository-patch-validation.md)를 함께 확인한다.

```json
{
  "repositoryUrl": "https://github.com/example/frontend.git",
  "commitSha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "repositoryCommits": {"backend": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"},
  "repositoryPatches": {
    "backend": {
      "commitSha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "patchDiff": "<unified diff relative to backend>"
    }
  }
}
```

- `repositoryCommits`는 수정하지 않는 추가 레포도 최초 관측의 SHA로 고정한다.
- branch 이름이 바뀌어도 재검증 소스가 바뀌지 않는다. 설정에 이미 SHA가 있으면 같은 SHA여야 한다.
- 각 패치는 해당 레포 루트 기준이다. 선언하지 않은 레포, 잘못된 SHA, 보호 경로는 거절한다.
- 기준 레포와 추가 레포의 패치를 합쳐 기존 1,000,000 bytes 상한을 적용한다.
- 모든 레포의 `git apply --check`와 디코딩한 경로 검사를 먼저 수행한 뒤 적용한다.
- 기준 레포 패치를 함께 적용할 때 version 3의 레포 URL·branch·SHA 선언은 보존해야 한다.
- 매 회차 모든 소스를 새로 복제하고 누적 패치를 적용한 그룹을 다시 배포·검사한다. 이전 namespace를 수정하지 않는다.
- 실패해도 요청별 namespace·이미지·볼륨·추가 clone을 기존 수명주기로 정리한다.
- 결과 `source.repository_patches`는 alias별 기준 SHA·패치 SHA256·적용 여부를 제공한다. 패치 본문은 이 근거 필드에 넣지 않는다.
- 새 필드가 비어 있으면 기존 요청의 재사용 identity를 유지한다. SHA나 패치가 달라지면 별도 실행이다.

AI는 한 회차에 레포 하나를 수정하고 전체 기본 3회 예산 안에서 대상으로 전환할 수 있다.
Sandbox는 누적 패치를 실행할 뿐 수정 대상을 추정하거나 Judge 기준을 변경하지 않는다.
여러 레포의 API 계약을 한꺼번에 변경하는 협조적 수정은 지원 완료 범위가 아니다.
