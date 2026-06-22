interface CharacterCardProps {
  character: Record<string, unknown>
}

export default function CharacterCard({ character }: CharacterCardProps) {
  const name = String(character.name ?? "")
  const roleType = character.role_type ? String(character.role_type) : null
  const age = character.age != null ? String(character.age) : null
  const identity = character.identity ? String(character.identity) : null
  const personality = character.personality ? String(character.personality) : null

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 p-3 space-y-1">
      <div className="flex items-center justify-between">
        <span className="font-semibold text-gray-100">{name}</span>
        {roleType && (
          <span className="text-xs px-2 py-0.5 rounded-full bg-gray-700 text-gray-300">
            {roleType}
          </span>
        )}
      </div>
      {age && <p className="text-xs text-gray-500">年龄：{age}</p>}
      {identity && <p className="text-xs text-gray-500">身份：{identity}</p>}
      {personality && (
        <p className="text-xs text-gray-500 line-clamp-2">{personality}</p>
      )}
    </div>
  )
}
