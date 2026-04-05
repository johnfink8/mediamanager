import { ItemListQuery$data } from "./__generated__/ItemListQuery.graphql";

export type itemType = ItemListQuery$data["items"]["nodes"][number];

export interface MenuItemType {
    name: string;
    icon: JSX.Element;
    component: React.FC<{ menuItem: MenuItemType }>;
    tabIndex: number | undefined;
    typeName: string | undefined;
}
