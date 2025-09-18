export interface Operator {
    value: string;
    label: string;
}

export const OPERATORS: Operator[] = [
    { value: "eq", label: "Equals" },
    { value: "neq", label: "Does NOT equal" },
    { value: "in", label: "In list" },
    { value: "not_in", label: "Not in list" },
    { value: "lt", label: "Less than" },
    { value: "gt", label: "Greater than" },
    { value: "lte", label: "Less than or equal" },
    { value: "gte", label: "Greater than or equal" },
    { value: "contains", label: "Contains" },
    { value: "not_contains", label: "Does NOT contain" },
]; 