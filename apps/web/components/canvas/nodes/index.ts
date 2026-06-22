import { SmartNode } from "../SmartNode"
import { NODE_STYLES } from "../nodeStyles"

const types: Record<string, typeof SmartNode> = {}
for (const key of Object.keys(NODE_STYLES)) {
  types[key] = SmartNode
}
types.default = SmartNode

export const nodeTypes = types

export { SmartNode }
