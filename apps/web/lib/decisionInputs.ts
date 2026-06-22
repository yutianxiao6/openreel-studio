export type DecisionInputValues = Record<string, unknown>

export interface BuildDecisionInputsOptions {
  kind: string
  target?: string
  action: string
  feedback?: string
  values?: DecisionInputValues
  extra?: DecisionInputValues
}

export function buildDecisionInputs({
  kind,
  target,
  action,
  feedback,
  values,
  extra,
}: BuildDecisionInputsOptions): DecisionInputValues {
  const normalizedTarget = target || kind
  const normalizedValues: DecisionInputValues = {
    ...(values ?? {}),
  }
  if (!normalizedValues.action) normalizedValues.action = action
  if (!normalizedValues.target) normalizedValues.target = normalizedTarget
  if (feedback && !normalizedValues.feedback) normalizedValues.feedback = feedback

  return {
    kind,
    target: normalizedTarget,
    action,
    ...(feedback ? { feedback } : {}),
    values: normalizedValues,
    ...(extra ?? {}),
  }
}
