# オントロジー下書き

## オントロジーの概念定義
業務で使用する対象、項目、関係をまとめています。

#### オブジェクト型（Object Type）

##### 従業員 (`Employee`)
- 対応する表・列: ADMIN.EMPLOYEE
- 主識別子: EmployeeId
- 業務上の粒度: 従業員1人
- プロパティ: EmployeeId / EmployeeName / DepartmentId / Email / HireDate / Salary


#### プロパティ（Property）

##### 所属部門番号 (`DepartmentId`)
- 対応する表・列: ADMIN.EMPLOYEE.DEPARTMENT_ID
- 対象オブジェクト: Employee
- データ型: number
- 必須: いいえ
- 更新可能: いいえ

##### メールアドレス (`Email`)
- 対応する表・列: ADMIN.EMPLOYEE.EMAIL
- 対象オブジェクト: Employee
- データ型: string
- 必須: いいえ
- 更新可能: いいえ

##### 従業員番号 (`EmployeeId`)
- 対応する表・列: ADMIN.EMPLOYEE.EMPLOYEE_ID
- 対象オブジェクト: Employee
- データ型: number
- 必須: いいえ
- 更新可能: いいえ

##### 氏名 (`EmployeeName`)
- 対応する表・列: ADMIN.EMPLOYEE.EMPLOYEE_NAME
- 対象オブジェクト: Employee
- データ型: string
- 必須: いいえ
- 更新可能: いいえ

##### 入社日 (`HireDate`)
- 対応する表・列: ADMIN.EMPLOYEE.HIRE_DATE
- 対象オブジェクト: Employee
- データ型: date
- 必須: いいえ
- 更新可能: いいえ

##### 給与 (`Salary`)
- 対応する表・列: ADMIN.EMPLOYEE.SALARY
- 対象オブジェクト: Employee
- データ型: number
- 必須: いいえ
- 更新可能: いいえ
