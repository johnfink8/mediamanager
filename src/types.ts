import { ItemListQuery$data } from "./__generated__/ItemListQuery.graphql";

export type itemType = ItemListQuery$data["items"]["nodes"][number];

export type TempFilterType = {
    attribute: string;
    operator: string;
    value: string;
};

export interface MenuItemType {
    name: string;
    icon: JSX.Element;
    component: React.FC<{ menuItem: MenuItemType }>;
    tabIndex: number | undefined;
    typeName: string | undefined;
}
