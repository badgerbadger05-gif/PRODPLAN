import { NavLink } from 'react-router-dom'

const tabs = [
  { to: '/dbr', title: 'Барабан', end: true },
  { to: '/dbr/programs', title: 'Программы' },
  { to: '/dbr/feeder', title: 'Питающий контур' },
  { to: '/dbr/purchase', title: 'Закупка' },
  { to: '/dbr/settings', title: 'Настройки' },
]

// Section-level navigation shared by all DBR pages (board / programs / settings).
export function DbrNav() {
  return (
    <nav className="dbrTabs" aria-label="Разделы планирования DBR">
      {tabs.map((tab) => (
        <NavLink
          key={tab.to}
          to={tab.to}
          end={tab.end}
          className={({ isActive }) => `dbrTab${isActive ? ' active' : ''}`}
        >
          {tab.title}
        </NavLink>
      ))}
    </nav>
  )
}
