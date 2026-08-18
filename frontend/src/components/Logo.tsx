// Offizielles KKPKS-PTI-Logo (Brand-Asset, identisch zu pti-crm/pti-translator).
export default function Logo() {
  return (
    <div className="flex items-center gap-3 select-none">
      <img src="/logo-de.jpg" alt="KKPKS PTI" className="h-9 w-auto" />
      <span className="text-sm font-semibold text-gray-400 dark:text-gray-500 border-l border-gray-300 dark:border-gray-600 pl-3">
        Pseudonymisierer
      </span>
    </div>
  );
}
